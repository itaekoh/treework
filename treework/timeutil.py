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
