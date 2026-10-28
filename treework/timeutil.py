"""시간대 처리.

공고의 모든 날짜·시각은 **KST 기준**으로 표기돼 있다. 그런데 GitHub Actions
러너는 UTC 로 돌기 때문에 `datetime.now()` 를 쓰면 9시간 이르게 판단한다.

실측 버그(2026-08-05): 마감이 `2026-08-05 10:00`(KST)인 나라장터 공고가
12:31 KST 실행에서 '아직 유효'로 판정돼 발송됐다. 러너의 `now()` 가 03:31(UTC)
이었기 때문이다. 로컬(KST)에서는 정상 동작해 개발 중에 드러나지 않았다.

그래서 '지금'이 필요한 모든 곳은 이 모듈을 쓴다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """KST 기준 현재 시각 (naive — 공고 문자열과 직접 비교하기 위해)."""
    return datetime.now(KST).replace(tzinfo=None)


def today_kst() -> date:
    """KST 기준 오늘 날짜."""
    return now_kst().date()


# ── 업무시간 판정 ────────────────────────────────────────────────────
#  GitHub cron 은 신뢰할 수 없다. 실측(2026-10): 예정 시각보다 **4~7시간**
#  늦게 실행돼 최근 24회 중 17회가 업무시간 밖(20~23시 KST)이었고 주말에도
#  돌았다. cron 식에 요일을 거는 것만으로는 집행되지 않는다.
#  → 실행 시점에 직접 확인해 업무시간이 아니면 아무 일도 하지 않는다.
BIZ_START, BIZ_END = 9, 18        # 09:00 ~ 18:00 KST


def korean_holidays(year: int) -> set:
    """해당 연도 한국 공휴일. holidays 패키지가 없으면 빈 집합(=평일 취급)."""
    try:
        import holidays as _h
        return set(_h.country_holidays("KR", years=[year]))
    except Exception:                                  # noqa: BLE001
        return set()


def is_business_day(d: date) -> bool:
    """정부기관이 일하는 날인가. 주말·공휴일이면 False."""
    return d.weekday() < 5 and d not in korean_holidays(d.year)


def business_status(now: datetime | None = None) -> tuple[bool, str]:
    """지금이 평일 업무시간인가. (판정, 사유) 반환.

    정부기관은 주말·공휴일에 공고를 올리지 않으므로 그 시간에 도는 것은
    낭비이고, 알림만 늘린다.
    """
    now = now or now_kst()
    wd = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    if now.weekday() >= 5:
        return False, f"주말({wd})"
    if now.date() in korean_holidays(now.year):
        return False, f"공휴일({now:%m-%d})"
    if not (BIZ_START <= now.hour < BIZ_END):
        return False, f"업무시간 밖({now:%H:%M} KST)"
    return True, f"{wd} {now:%H:%M} KST"
