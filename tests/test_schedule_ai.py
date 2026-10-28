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

# 백엔드 선택 — API 키가 있으면 CLI 는 쳐다보지도 않는다.
# (CI 러너에 claude CLI 가 없어도 API 경로는 멀쩡히 돌아야 한다)
import shutil                                 # noqa: E402

_env_backup = {k: os.environ.get(k) for k in
               ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TREEWORK_AI")}


def _with_env(**kv) -> str:
    for k in _env_backup:
        os.environ.pop(k, None)
    for k, v in kv.items():
        if v is not None:
            os.environ[k] = v
    try:
        return ai.backend()
    finally:
        for k, v in _env_backup.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


check("[백엔드] API 키가 있으면 api", _with_env(ANTHROPIC_API_KEY="sk-x"), "api")
check("[백엔드] 둘 다 있으면 api 가 이긴다(싸다)",
      _with_env(ANTHROPIC_API_KEY="sk-x", CLAUDE_CODE_OAUTH_TOKEN="t"), "api")
check("[백엔드] 아무것도 없으면 빈 문자열", _with_env(), "")
check("[백엔드] off 스위치가 모든 것을 이긴다",
      _with_env(ANTHROPIC_API_KEY="sk-x", TREEWORK_AI="off"), "")
# 구독 토큰만 있을 때는 CLI 설치 여부가 가른다. 토큰만 믿고 호출하면
# CI 에서 매 실행 실패하고 경고만 쌓인다.
check("[백엔드] 구독 토큰만 있을 때는 CLI 유무를 본다",
      _with_env(CLAUDE_CODE_OAUTH_TOKEN="t"),
      "cli" if shutil.which("claude") else "")

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


# ── 수도권 지역 필터 ──────────────────────────────────────────────
# 나라장터 API 의 지역 파라미터가 무시되므로 기관명으로 거른다. 여기서
# 실수하면 조용히 누락되거나(수도권 건을 버림) 소음이 된다(타지역을 받음).
import yaml                                     # noqa: E402
from pathlib import Path                        # noqa: E402
from treework.collectors import region_verdict  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_SRC = {s["id"]: s for s in
        yaml.safe_load(open(ROOT / "sources.yml", encoding="utf-8"))["sources"]}
RW = _SRC["g2b-it"]["region_keywords"]
KU = _SRC["g2b-it"].get("keep_region_unknown", True)


def verdict(org: str, demand: str = "") -> str:
    return region_verdict(org, demand, RW, KU)


check("[지역] 서울 기관은 통과", verdict("서울특별시 성동구"), "hit")
check("[지역] 인천 기관은 통과", verdict("인천광역시 상수도사업본부"), "hit")
check("[지역] 경기도청은 통과", verdict("경기도 교육청"), "hit")
# 경기는 '경기'가 안 들어간 시·군 이름만 쓰는 기관이 많다. 이게 핵심이다.
check("[지역] 수원시청은 통과(시군명만 있어도)", verdict("수원시"), "hit")
check("[지역] 성남도시개발공사는 통과", verdict("성남도시개발공사"), "hit")
check("[지역] 여주시는 통과", verdict("여주시청"), "hit")
check("[지역] 연천군은 통과", verdict("연천군"), "hit")
# 광주는 둘이다. 경기 광주시는 받고 광주광역시는 버려야 한다.
check("[지역] 경기 광주시는 통과", verdict("광주시청"), "hit")
check("[지역] 광주광역시는 제외", verdict("광주광역시청"), "other")
check("[지역] 부산은 제외", verdict("부산광역시 기장군"), "other")
check("[지역] 제주는 제외", verdict("제주특별자치도"), "other")
# 수요기관 쪽에만 지역이 있어도 받아야 한다(조달청 대행 발주).
check("[지역] 발주처가 조달청이어도 수요기관이 서울이면 통과",
      verdict("조달청", "서울특별시 도시기반시설본부"), "hit")
# 지역을 알 수 없는 기관은 버리지 않는다 — 버리면 조용한 누락이 된다.
check("[지역] 기관명에 지역이 없으면 남긴다", verdict("국회사무처"), "keep")
check("[지역] 한국도로공사도 남긴다", verdict("한국도로공사"), "keep")
check("[지역] keep_region_unknown=False 면 버린다",
      region_verdict("국회사무처", "", RW, False), "drop")
check("[지역] region_keywords 가 비면 전국을 받는다",
      region_verdict("부산광역시", "", [], True), "hit")
# 경기 31개 시군이 빠짐없이 들어 있는가. 하나라도 빠지면 그 시의 공고가
# 영영 오지 않는데, 안 오는 것은 눈에 띄지 않는다.
GYEONGGI = ["수원시", "고양시", "용인시", "성남시", "부천시", "화성시", "안산시",
            "남양주시", "안양시", "평택시", "시흥시", "파주시", "의정부시", "김포시",
            "광주시", "광명시", "군포시", "하남시", "오산시", "양주시", "이천시",
            "구리시", "안성시", "포천시", "의왕시", "여주시", "양평군", "동두천시",
            "과천시", "가평군", "연천군"]
missed = [c for c in GYEONGGI if verdict(c) != "hit"]
check("[지역] 경기 31개 시군이 모두 통과한다", missed, [])

# ── IT 오탐: 토목·설비 유지관리 ───────────────────────────────────
# '유지관리용역'·'유지보수'가 B티어라 토목 건이 그 한 단어로 들어왔다.
# 다만 A티어 신호가 있으면 살려야 한다 — 겹치는 제목이 실제로 흔하다.
def it_pass(title: str) -> bool:
    m = filters.classify(title, profile="it")
    return filters.is_actionable(title, m.tier, "B", profile="it")


check("[IT] 상수도관망 유지관리는 제외",
      it_pass("하동군 상수도관망 전문유지관리 용역"), False)
check("[IT] 지하수시설 유지관리는 제외",
      it_pass("장산1외 2지구 공사관리 지하수시설 유지관리용역"), False)
check("[IT] 냉난방시설 유지보수는 제외",
      it_pass("전문대학 혁신지원사업 관련 교내 냉난방시설 유지보수"), False)
check("[IT] 토목 단어가 있어도 A티어면 살린다",
      it_pass("이문-2 소구역 하수관로 정비공사 GIS DB 구축 용역"), True)
check("[IT] 하천관리 시스템구축도 살린다",
      it_pass("하천 통합관리 정보시스템 구축 용역"), True)
check("[IT] 평범한 정보시스템 건은 그대로 통과",
      it_pass("2027년 행정정보시스템 통합 유지관리 용역"), True)
# 수목 프로파일은 이 규칙에 영향받지 않아야 한다.
check("[수목] 상수도 단어가 수목 공고를 막지 않는다",
      filters.is_actionable("상수도사업본부 수목전정 및 병해충방제 용역",
                            filters.classify(
                                "상수도사업본부 수목전정 및 병해충방제 용역").tier,
                            "C"), True)

print(f"\n실패 {fails}건")
sys.exit(1 if fails else 0)
