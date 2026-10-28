"""침묵 감시 회귀 테스트.

이 장치는 **평소에 아무 말도 하지 않는 것이 정상**이라 고장나도 티가 안 난다.
그래서 판정 로직만큼은 테스트로 묶어 둔다. 특히 두 방향의 실패가 모두 치명적이다.

  · 놓치면   — 수집이 멈춰도 아무도 모른다 (이 장치의 존재 이유가 사라진다)
  · 오경보   — 몇 번 헛울리면 알림을 믿지 않게 되고, 결국 놓치는 것과 같아진다

    python tests/test_watchdog.py
"""

from __future__ import annotations

import io
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import watchdog                                   # noqa: E402

fails = 0


def check(desc: str, got, want) -> None:
    global fails
    if got == want:
        print(f"PASS  {desc}")
    else:
        fails += 1
        print(f"FAIL  {desc}\n      기대={want} 실제={got}")


def run(created_at: str, conclusion: str = "success") -> dict:
    return {"created_at": created_at, "conclusion": conclusion}


# ── 마지막 실행 시각 (UTC → KST) ──────────────────────────────────
check("[시각] UTC 를 KST 로 바꾼다",
      watchdog.last_fired([run("2026-10-26T05:32:14Z")]),
      datetime(2026, 10, 26, 14, 32, 14))
check("[시각] 자정을 넘기는 변환",
      watchdog.last_fired([run("2026-10-26T15:10:00Z")]),
      datetime(2026, 10, 27, 0, 10, 0))
check("[시각] 여러 건이면 가장 최근",
      watchdog.last_fired([run("2026-10-20T00:00:00Z"),
                           run("2026-10-26T00:00:00Z"),
                           run("2026-10-22T00:00:00Z")]),
      datetime(2026, 10, 26, 9, 0, 0))
# 실패해도 '떴다'는 증거다. 떠서 깨진 것은 기존 failure() 알림이 담당한다.
check("[시각] 실패한 실행도 '떴다'로 센다",
      watchdog.last_fired([run("2026-10-26T00:00:00Z", "failure")]),
      datetime(2026, 10, 26, 9, 0, 0))
# 취소는 떴지만 아무것도 수집하지 않았다.
check("[시각] 취소된 실행은 세지 않는다",
      watchdog.last_fired([run("2026-10-26T00:00:00Z", "cancelled")]), None)
check("[시각] 실행이 없으면 None", watchdog.last_fired([]), None)
check("[시각] 날짜 형식이 깨져도 죽지 않는다",
      watchdog.last_fired([run("언젠가"), run("2026-10-26T00:00:00Z")]),
      datetime(2026, 10, 26, 9, 0, 0))

# ── 빠진 영업일 ───────────────────────────────────────────────────
# 2026-10-26 월 / 27 화 / 28 수 / 29 목 / 30 금 / 31 토 / 11-01 일 / 02 월
md = watchdog.missed_business_days

check("[영업일] 오늘 돌았으면 빠진 날 없음",
      md(datetime(2026, 10, 28, 9, 0), datetime(2026, 10, 28, 18, 0)), [])
check("[영업일] 오늘은 아직 안 끝났으므로 세지 않는다",
      md(datetime(2026, 10, 27, 23, 0), datetime(2026, 10, 28, 18, 0)), [])
check("[영업일] 어제 하루가 통째로 빠지면 잡는다",
      md(datetime(2026, 10, 26, 14, 0), datetime(2026, 10, 28, 10, 0)),
      [date(2026, 10, 27)])
check("[영업일] 이틀 빠지면 둘 다",
      md(datetime(2026, 10, 26, 14, 0), datetime(2026, 10, 29, 10, 0)),
      [date(2026, 10, 27), date(2026, 10, 28)])

# 오경보 방지의 핵심. 금요일 마지막 실행과 월요일 사이는 정상이어도
# 70시간이 넘는다. 벽시계로 자르면 매주 월요일 아침마다 헛울린다.
check("[영업일] 주말은 빠진 날이 아니다 (금→월)",
      md(datetime(2026, 10, 30, 17, 0), datetime(2026, 11, 2, 10, 0)), [])
check("[영업일] 주말을 건너뛴 뒤 월요일이 빠지면 잡는다",
      md(datetime(2026, 10, 30, 17, 0), datetime(2026, 11, 3, 10, 0)),
      [date(2026, 11, 2)])
# 공휴일도 마찬가지. 2026-10-09 는 한글날(금).
check("[영업일] 공휴일은 빠진 날이 아니다 (한글날)",
      md(datetime(2026, 10, 8, 17, 0), datetime(2026, 10, 12, 10, 0)), [])

# cron 이 7시간 밀려 자정을 넘겨도, 그날 실행이 있었다면 다음 날 판정은 깨끗하다.
check("[영업일] 7시간 밀려 자정 넘긴 실행도 정상으로 본다",
      md(datetime(2026, 10, 28, 0, 10), datetime(2026, 10, 28, 20, 0)), [])

# 기록이 아예 없으면(조회 범위 밖 = 아주 오래 멈춤) 침묵하면 안 된다.
none_case = md(None, datetime(2026, 10, 28, 10, 0))
check("[영업일] 실행 기록이 없으면 빠진 날로 본다", len(none_case) > 0, True)

# ── 보고문 ────────────────────────────────────────────────────────
msg = watchdog.build_report(
    [("나라장터 수집", [date(2026, 10, 27)], datetime(2026, 10, 26, 14, 32))],
    datetime(2026, 10, 28, 10, 0))
check("[보고] 무엇이 멈췄는지 이름이 들어간다", "나라장터 수집" in msg, True)
check("[보고] 빠진 날짜가 들어간다", "10/27" in msg, True)
check("[보고] 마지막 실행 시각이 들어간다", "10/26 14:32" in msg, True)
check("[보고] 바로 할 수 있는 일이 적혀 있다", "Run workflow" in msg, True)

hb = watchdog.build_heartbeat([("수목 공고 수집", datetime(2026, 11, 2, 9, 10))],
                              datetime(2026, 11, 2, 9, 30))
check("[생존신호] 정상임을 알린다", "감시 정상" in hb, True)
check("[생존신호] 마지막 실행을 함께 보여준다", "11/02 09:10" in hb, True)

# ── 감시 장치 자신의 생존 확인 ────────────────────────────────────
# 조회 실패를 '죽었다'로 보고하면 오경보가 된다. 그 구분을 지킨다.
_orig = watchdog.fetch_runs
try:
    watchdog.fetch_runs = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("API 장애"))
    check("[생존] 조회에 실패하면 None (모른다 ≠ 죽었다)",
          watchdog.silent_hours(), None)

    watchdog.fetch_runs = lambda *a, **k: []
    check("[생존] 실행 기록이 없어도 None", watchdog.silent_hours(), None)

    watchdog.fetch_runs = lambda *a, **k: [run("2026-10-26T00:00:00Z")]
    got = watchdog.silent_hours(now=datetime(2026, 10, 28, 9, 0))
    check("[생존] 마지막 실행 이후 경과 시간을 준다", got, 48.0)
finally:
    watchdog.fetch_runs = _orig

print(f"\n실패 {fails}건")
sys.exit(1 if fails else 0)
