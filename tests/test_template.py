"""텔레그램 메시지 템플릿 회귀 테스트.

읽는 순서를 고정한다. 제목 → 누가 → 언제까지·얼마 → 왜 → AI → 링크.
이 순서가 흐트러지면 목록을 훑을 수 없게 된다.

마감 표기는 **남은 날짜**여야 한다. '2026-10-30' 만으로는 급한지 알 수 없고,
자치구 기간제 공고는 접수기간이 3~5일로 짧다.

    python tests/test_template.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from treework import notify     # noqa: E402

fails = 0


def check(desc: str, got, want) -> None:
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {desc}")
    if not ok:
        print(f"      기대={want!r} 실제={got!r}")


# ── 마감 표기 ──────────────────────────────────────────────────────
NOW = datetime(2026, 10, 27, 11, 0)
DEADLINES = [
    ("오늘 마감은 불이 붙는다", "2026-10-27 18:00", "🔥", "오늘 마감"),
    ("내일 마감도 불이 붙는다", "2026-10-28", "🔥", "내일 마감"),
    ("D-2 는 모래시계", "2026-10-29", "⏳", "D-2"),
    ("D-3 까지 모래시계", "2026-10-30", "⏳", "D-3"),
    ("여유 있으면 달력", "2026-11-10", "🗓", "D-14"),
    ("지난 건은 지났다고", "2026-10-20", "🗓", "지남"),
    ("마감을 모르면 모른다고", "", "🗓", "미표기"),
    ("못 읽는 형식도 버리지 않는다", "접수시 확인", "🗓", "접수시 확인"),
]
for desc, due, icon, text in DEADLINES:
    got = notify._deadline(due, NOW)
    check(f"[마감] {desc}", got.startswith(icon) and text in got, True)


# ── 공고 블록의 줄 순서 ────────────────────────────────────────────
FULL = {
    "title": "2026년 소나무재선충병 예방나무주사 사업", "tier": "A",
    "org": "서울특별시", "dept": "녹지관리과", "region": "서울",
    "due_date": "2026-12-31", "amount": "1.2억원",
    "hits": ["재선충(A)", "방제(B)"], "attach_names": ["a", "b", "c"],
    "ai_fit": 5, "ai_why": "나무의사 핵심 업무",
    "link": "http://ex.kr/a", "doc_url": "http://ex.kr/b", "ref_no": "2026-1",
}
rows = notify.format_posting(FULL).split("\n")
check("[순서] 1행은 제목", rows[0], "🔴 <b>2026년 소나무재선충병 예방나무주사 사업</b>")
check("[순서] 2행은 기관·부서·지역", rows[1],
      "🏢 서울특별시 · 녹지관리과 · 📍서울")
check("[순서] 3행은 마감·금액", rows[2].startswith("🗓") and "💰 1.2억원" in rows[2], True)
check("[순서] 4행은 근거", rows[3], "🔎 재선충 · 방제 · 📎3")
check("[순서] 5행은 AI", rows[4], "🤖 <b>5</b>/5 · 나무의사 핵심 업무")
check("[순서] 6행은 링크", rows[5].startswith("🔗") and "ex.kr/a" in rows[5], True)
check("[순서] 7행은 공고번호(복사용 code)", rows[6], "🔢 <code>2026-1</code>")

# 등급 꼬리표는 점으로 대신한다 — 제목을 밀어내지 않게.
check("[순서] 근거에 등급 꼬리표가 남지 않는다", "(A)" in rows[3], False)

# ── 없는 정보는 자리를 차지하지 않는다 ────────────────────────────
MIN = {"title": "공고", "tier": "C", "org": "노원구", "link": "http://z"}
mrows = notify.format_posting(MIN).split("\n")
# 마감 줄은 비어도 남긴다 — "모른다"는 것도 알아야 할 정보다.
check("[간결] 최소 정보는 4줄(제목·기관·마감·링크)", len(mrows), 4)
check("[간결] 마감을 모르면 모른다고 적는다", mrows[2], "🗓 마감 미표기")
check("[간결] 금액이 없으면 💰 가 없다", "💰" in mrows[1], False)
check("[간결] 지역이 없으면 📍 가 없다", "📍" in mrows[1], False)
check("[간결] AI 가 없으면 🤖 가 없다", "🤖" in "".join(mrows), False)

# ── 판정을 흔드는 사정은 경고로 ───────────────────────────────────
WARN = dict(MIN, demoted=True, link_is_board=True, attach_truncated=True)
wtext = notify.format_posting(WARN)
for word in ("등급 하향", "게시판에서 검색", "첨부 일부만 판독"):
    check(f"[경고] {word} 를 알린다", word in wtext, True)


# ── 평문 폴백 — 태그가 깨져도 내용과 주소는 도착한다 ──────────────
plain = notify.to_plain(notify.format_posting(FULL))
check("[폴백] 태그가 사라진다", "<" in plain, False)
check("[폴백] 제목은 남는다", "소나무재선충병" in plain, True)
check("[폴백] 링크 주소가 살아 있다",
      "http://ex.kr/a" in plain and "http://ex.kr/b" in plain, True)
check("[폴백] HTML 이스케이프가 풀린다",
      "&" in notify.to_plain(notify.format_posting(dict(MIN, title="가 & 나"))), True)


# ── 머리글 ────────────────────────────────────────────────────────
HEALTH = {"sources": [], "ok": 40, "total": 40, "scanned": 704}
msgs = notify.build_messages(
    [dict(FULL, profile="tree"),
     {"title": "정보시스템 구축", "tier": "A", "profile": "it",
      "org": "조달청", "link": "http://i"}], HEALTH)
check("[머리글] 업역이 두 메시지로 갈린다", len(msgs), 2)
check("[머리글] 수목이 먼저", msgs[0].startswith("🌳"), True)
check("[머리글] IT 가 다음", msgs[1].startswith("💻"), True)
check("[머리글] 건수가 첫 줄에", "신규 1건" in msgs[0].split("\n")[0], True)
check("[머리글] 등급 집계가 둘째 줄에", "🔴 1" in msgs[0].split("\n")[1], True)

print(f"\n실패 {fails}건")
sys.exit(1 if fails else 0)
