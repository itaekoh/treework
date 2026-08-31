"""알림 게이팅 회귀 테스트.

핵심 규칙: **1회 실패는 알리지 않는다.** 하루 4~9회 돌고 lookback 이 겹치므로
다음 실행이 메우고, 매번 알리면 경고가 일상이 되어 진짜 고장을 놓친다.
연속 2회 이상, 0건 반환, 수집량 급감은 반드시 알린다.

    python tests/test_notify.py
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from treework import notify  # noqa: E402


def src(name, status="ok", rows=10, fails=0, drop=False, err="",
        alert=None, recovered=False, chronic=None):
    """alert 를 생략하면 state.record_health 의 규칙을 그대로 흉내낸다."""
    if chronic is None:
        chronic = fails >= 5
    if alert is None:
        alert = fails >= 2 and not chronic     # 만성은 하루 1회 -> 보통은 억제
    return {"id": name, "name": name, "status": status, "rows": rows,
            "error": err,
            "diag": {"consecutive_failures": fails, "days_since_ok": None,
                     "volume_drop": drop, "baseline": 50 if drop else None,
                     "alert": alert, "recovered": recovered, "chronic": chronic}}


def health(sources):
    ok = sum(1 for s in sources if s["status"] == "ok")
    return {"sources": sources, "ok": ok, "total": len(sources), "scanned": 100}


CASES = [
    ("1회 실패는 경고하지 않는다 (다음 실행이 메운다)",
     [src("정상"), src("나라장터", "error", 0, fails=1, err="ConnectTimeout")],
     False, "알림 억제"),

    ("연속 2회 실패는 경고한다",
     [src("정상"), src("나라장터", "error", 0, fails=2, err="ConnectTimeout")],
     True, "수집 실패"),

    ("연속 3회는 🚨 로 승격한다",
     [src("정상"), src("중랑구", "error", 0, fails=3, err="ConnectTimeout")],
     True, "🚨"),

    ("0건 반환은 1회라도 즉시 경고한다 (구조 변경 신호)",
     [src("정상"), src("어떤구", "ok", rows=0)],
     True, "0건 반환"),

    ("수집량 급감은 경고한다",
     [src("정상"), src("어떤구", "ok", rows=3, drop=True)],
     True, "급감"),

    ("전부 정상이면 ✅ 만 보낸다",
     [src("가"), src("나")],
     False, "정상 동작"),

    ("미설정(skipped)은 고장이 아니다",
     [src("정상"), src("나라장터", "skipped", 0)],
     False, "미설정"),

    ("단일 소스가 1회 실패해도 조용하다 (나라장터 전용 워크플로우)",
     [src("나라장터", "error", 0, fails=1, err="ConnectTimeout")],
     False, "알림 억제"),

    # ── 만성 고장 억제 (실측: 중랑구 83회 연속 실패를 83번 알렸다) ──
    ("만성 고장(5회 이상)은 그날 이미 알렸으면 조용하다",
     [src("정상"), src("중랑구", "error", 0, fails=83, err="ConnectTimeout")],
     False, "알림 억제"),

    ("만성 고장이라도 그날 첫 알림은 보낸다",
     [src("정상"),
      src("중랑구", "error", 0, fails=83, err="ConnectTimeout", alert=True)],
     True, "만성(하루 1회만 알림)"),

    ("고장났던 소스가 복구되면 알린다",
     [src("정상"), src("양천구", "ok", rows=50, recovered=True, fails=6)],
     True, "복구됨"),
]


def main() -> int:
    fails = 0
    for desc, sources, want_warn, must_contain in CASES:
        msgs = notify.build_messages([], health(sources))
        text = "\n".join(msgs)
        warned = "수집 상태 경고" in text and "정상 동작" not in text
        ok = (warned == want_warn) and (must_contain in text)
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"      경고={warned}(기대 {want_warn}) "
                  f"'{must_contain}' 포함={must_contain in text}")
            print(f"      실제: {text[:200]!r}")
    print(f"\n실패 {fails}건")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
