"""업무시간 가드 · IT 프로파일 · AI 선별 계층 회귀 테스트.

세 가지를 고정한다.

1. 주말·공휴일·야간에는 수집이 돌지 않는다 (정부기관이 공고를 올리지 않는다).
2. IT 용역은 수목과 **별개 프로파일**로 분류되고, 겹치는 건은 양쪽 모두에서
   사라지지 않는다 (예: '공원 수목관리 시스템 구축').
3. AI 가 없거나 실패해도 공고는 한 건도 사라지지 않는다 (fail-open).
   이 수집기의 목적은 누락을 없애는 것이고 AI 는 읽는 부담을 줄이는 보조다.

    python tests/test_schedule_ai.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import main                                   # noqa: E402
from treework import ai, filters, notify     # noqa: E402
from treework.collectors import Posting       # noqa: E402
from treework.timeutil import business_status  # noqa: E402

fails = 0


def check(desc: str, got, want) -> None:
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {desc}")
    if not ok:
        print(f"      기대={want!r} 실제={got!r}")


# ── 1. 업무시간 ────────────────────────────────────────────────────
#  날짜는 2026년 실제 달력. 설·추석은 음력이라 holidays 패키지가 필요하다.
SCHEDULE = [
    ("평일 오전", datetime(2026, 10, 27, 10, 30), True),
    ("평일 09:00 정각(창의 시작)", datetime(2026, 10, 27, 9, 0), True),
    ("평일 17:59(창의 끝)", datetime(2026, 10, 27, 17, 59), True),
    ("평일 18:00 — 업무 종료", datetime(2026, 10, 27, 18, 0), False),
    ("평일 08:59 — 업무 시작 전", datetime(2026, 10, 27, 8, 59), False),
    ("토요일", datetime(2026, 10, 24, 11, 0), False),
    ("일요일", datetime(2026, 10, 25, 14, 0), False),
    ("한글날(10/9)", datetime(2026, 10, 9, 11, 0), False),
    ("추석 연휴(음력 계산 필요)", datetime(2026, 9, 25, 11, 0), False),
    ("개천절 대체공휴일(10/5)", datetime(2026, 10, 5, 11, 0), False),
]
for desc, when, want in SCHEDULE:
    check(f"[업무시간] {desc}", business_status(when)[0], want)

# holidays 패키지가 빠지면 공휴일 판정이 조용히 무력화된다 — 그러면 공휴일에도
# 돌지만 수집 자체는 정상이다. 설치 여부를 눈에 보이게 고정해 둔다.
check("[업무시간] holidays 패키지로 2026 공휴일을 읽는다",
      len(filters.__name__) > 0 and bool(__import__("treework.timeutil",
      fromlist=["korean_holidays"]).korean_holidays(2026)), True)


# ── 2. IT 프로파일 ────────────────────────────────────────────────
#  (설명, 제목, 프로파일, 기대 통과 여부). min_tier 는 나라장터 실제값 'B'.
IT_CASES = [
    ("정보시스템 구축은 IT 로 잡힌다", "통합정보시스템 구축 용역", "it", True),
    ("전산 유지보수도 잡힌다", "노후 전산장비 유지보수 용역", "it", True),
    ("IT 감리는 제외(수주 불가 업역)", "정보시스템 감리 용역", "it", False),
    ("평가위원 모집은 제외", "홈페이지 개편 평가위원 공개모집", "it", False),
    ("순수 조경 공사는 IT 가 아니다", "도시공원 조경식재 공사", "it", False),
    ("행사 대행은 '시스템'이 있어도 제외",
     "구민의 날 행사 대행 및 운영시스템 용역", "it", False),
    # 겹치는 업역 — 양쪽 모두에서 사라지면 안 된다.
    ("수목+IT 겹치는 건은 IT 쪽에서 잡힌다",
     "공원 수목관리 시스템 구축 용역", "it", True),
    ("수목 공고는 IT 프로파일로는 안 잡힌다", "소나무재선충병 방제사업", "it", False),
    ("IT 공고는 수목 프로파일로는 안 잡힌다", "통합정보시스템 구축 용역", "tree", False),
]
for desc, title, prof, want in IT_CASES:
    m = filters.classify(title, profile=prof)
    got = filters.is_actionable(title, m.tier, "B", profile=prof)
    check(f"[IT] {desc}", got, want)

# 기본 프로파일은 수목이다 — 기존 호출부가 인자 없이 그대로 동작해야 한다.
check("[IT] profile 생략 시 수목 규칙",
      filters.classify("소나무재선충병 방제사업").tier, "A")


# ── 2b. 업역 중복 ─────────────────────────────────────────────────
#  나라장터는 같은 API 를 프로파일만 바꿔 두 번 읽는다. 양쪽 규칙에 모두
#  걸리는 공고가 두 번 발송되면 안 되고, 남는 쪽은 본업인 수목이어야 한다.
BOTH = "가로수 생육환경 전수조사 및 DB구축"
check("[중복] 수목 프로파일로 걸린다",
      filters.is_actionable(BOTH, filters.classify(BOTH).tier, "B"), True)
check("[중복] IT 프로파일로도 걸린다",
      filters.is_actionable(BOTH, filters.classify(BOTH, profile="it").tier,
                            "B", profile="it"), True)

def _p(sid: str, title: str, prof: str):
    return Posting(source_id=sid, org="조달청", title=title,
                   reg_date="2026-10-20", profile=prof)


_kept = main.dedupe_profiles([
    _p("g2b-it", BOTH, "it"),
    _p("g2b-servc", BOTH, "tree"),
    _p("g2b-it", "통합정보시스템 구축 용역", "it"),
])
check("[중복] 겹치는 건은 한 번만 남는다", len(_kept), 2)
check("[중복] 남는 쪽은 수목이다",
      next(x.profile for x in _kept if x.title == BOTH), "tree")
check("[중복] 겹치지 않는 IT 건은 그대로 남는다",
      any(x.title == "통합정보시스템 구축 용역" for x in _kept), True)


# ── 3. AI 선별 — fail-open ────────────────────────────────────────
POSTS = [
    {"title": "소나무재선충병 방제", "tier": "A", "profile": "tree",
     "org": "서울시", "link": "http://a"},
    {"title": "숲속작은도서관 위탁운영", "tier": "C", "profile": "tree",
     "org": "노원구", "link": "http://b"},
]
HEALTH = {"sources": [], "ok": 1, "total": 1, "scanned": 2}

check("[AI] 백엔드가 없으면 review 는 아무것도 하지 않는다",
      ai.review([dict(p) for p in POSTS]), "")
check("[AI] 점수가 없으면 보류로 내리지 않는다",
      any(ai.is_demoted(p) for p in POSTS), False)

# AI 가 꺼져 있을 때 두 건 모두 본문에 남아야 한다.
off = "\n".join(notify.build_messages([dict(p) for p in POSTS], HEALTH))
check("[AI] 꺼져 있으면 모든 공고가 본문에 나온다",
      all(p["title"] in off for p in POSTS), True)
check("[AI] 꺼져 있으면 보류 섹션이 없다", "AI 보류" in off, False)

# AI 가 낮게 본 건은 **본문에서 빠지되 사라지지는 않는다**.
scored = [dict(p) for p in POSTS]
scored[0].update(ai_fit=5, ai_why="핵심 업무")
scored[1].update(ai_fit=0, ai_why="도서관 운영")
on = "\n".join(notify.build_messages(scored, HEALTH))
check("[AI] 낮게 본 건도 제목은 반드시 남는다",
      "숲속작은도서관 위탁운영" in on, True)
check("[AI] 낮게 본 건은 보류 섹션으로 간다", "AI 보류" in on, True)
check("[AI] 높게 본 건은 점수와 함께 본문에",
      "<b>5</b>/5" in on and "핵심 업무" in on, True)
check("[AI] 신규 건수는 보류 포함 전체를 센다", "신규 2건" in on, True)

# 응답이 깨져도 예외를 올리지 않고, 일부만 와도 나머지는 통과시킨다.
partial = [dict(p) for p in POSTS]
try:
    ai._parse("쓰레기 응답", 2)
    check("[AI] 깨진 응답은 예외를 올린다(상위에서 잡아 통과시킨다)", True, False)
except ValueError:
    check("[AI] 깨진 응답은 예외를 올린다(상위에서 잡아 통과시킨다)", True, True)
check("[AI] 일부 행만 와도 나머지는 점수 없이 남는다",
      sorted(ai._parse('[{"i": 2, "fit": 1, "why": "x"}]', 2)), [2])


print(f"\n실패 {fails}건")
sys.exit(1 if fails else 0)
