"""전국 수집 관련 회귀 테스트.

나라장터는 지역 파라미터가 무시되므로(실측) 전국을 받아 로컬에서 거른다.
2026-08-31 부터 지역제한 없는 공고가 88% 라는 실측에 따라 전국으로 넓혔다.
`region_keywords: []` 가 '전국'을 뜻하는지, 시도 표기가 나오는지 고정한다.

    python tests/test_region.py
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import yaml  # noqa: E402

from treework.collectors import _sido  # noqa: E402
from treework import notify  # noqa: E402

SIDO_CASES = [
    ("서울특별시 서부공원여가센터", "서울"),
    ("서울특별시 강남구", "서울"),
    ("부산광역시 기장군", "부산"),
    ("경기도 수원시", "경기"),
    ("전라남도 순천시", "전남"),
    ("경상북도 안동시", "경북"),
    ("제주특별자치도", "제주"),
    ("산림청 북부지방산림청 서울국유림관리소", "서울"),
    # 중앙부처·공공기관은 이름에 지역이 없다 (실측 판별율 약 49%)
    ("외교부", ""),
    ("국토교통부 국토지리정보원", ""),
    ("한국임업진흥원", ""),
]


def check_sido() -> int:
    fails = 0
    for name, want in SIDO_CASES:
        got = _sido(name)
        if got != want:
            print(f"FAIL  시도 추출: {name!r} 기대={want!r} 실제={got!r}")
            fails += 1
    print(f"{'PASS' if not fails else 'FAIL'}  시도 추출 "
          f"{len(SIDO_CASES) - fails}/{len(SIDO_CASES)}")
    return fails


def check_config() -> int:
    """sources.yml 이 실제로 전국 설정인지."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = yaml.safe_load(open(os.path.join(root, "sources.yml"), encoding="utf-8"))
    g = next(x for x in d["sources"] if x["id"] == "g2b-servc")
    fails = 0
    if g.get("region_keywords") != []:
        print(f"FAIL  나라장터가 전국이 아니다: {g.get('region_keywords')!r}")
        fails += 1
    if "min_amount" not in g:
        print("FAIL  min_amount 조절 장치가 없다")
        fails += 1
    print(f"{'PASS' if not fails else 'FAIL'}  전국 설정 확인")
    return fails


def check_message() -> int:
    """메시지에 지역이 표기되는지."""
    p = {"title": "○○시 수목 진단 용역", "org": "전라남도 순천시",
         "tier": "A", "hits": ["수목진단(A)"], "region": "전남",
         "reg_date": "2026-08-31", "due_date": "2026-09-10 10:00",
         "amount": "3,200만원", "ref_no": "R26BK00000001-000"}
    text = notify.format_posting(p)
    fails = 0
    if "전남" not in text.split("\n")[0]:
        print(f"FAIL  첫 줄에 지역이 없다: {text.splitlines()[0]!r}")
        fails += 1
    # 지역을 모르면 배지가 깨지지 않아야 한다
    p2 = dict(p, region="")
    if "·  <b>" in notify.format_posting(p2):
        print("FAIL  지역이 없을 때 배지가 어색하다")
        fails += 1
    print(f"{'PASS' if not fails else 'FAIL'}  메시지 지역 표기")
    return fails


def main() -> int:
    fails = check_sido() + check_config() + check_message()
    print(f"\n실패 {fails}건")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
