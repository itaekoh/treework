"""공공데이터포털 서비스키 정규화 회귀 테스트.

포털은 같은 키를 Encoding/Decoding 두 형태로 나란히 보여주고, Encoding 쪽을
쓰면 이중 인코딩으로 인증이 실패한다. 실제로 이 문제로 시간을 썼기 때문에
어느 쪽을 넣어도 동작하는 성질을 테스트로 고정한다.

    python tests/test_service_key.py
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from treework.collectors import normalize_service_key  # noqa: E402

# 실제 포털 키 형태를 모방한 값 (실키 아님)
DECODED = "5/kxQgtEJ3Sru4BUcGr6kQ+wcnqAzOeqsadzw9BYo2aApRk970EOBF/hlRN0fyxun=="
ENCODED = ("5%2FkxQgtEJ3Sru4BUcGr6kQ%2Bwcnqaz0eqsadzw9BYo2aApRk970EOBF"
           "%2FhlRN0fyxun%3D%3D")

CASES = [
    ("Encoding 키는 디코딩된다",
     ENCODED, True, lambda k: "%" not in k and "/" in k and k.endswith("==")),
    ("Decoding 키는 그대로 통과한다",
     DECODED, False, lambda k: k == DECODED),
    ("앞뒤 공백은 제거된다",
     f"  {DECODED}  ", False, lambda k: k == DECODED),
    ('감싼 따옴표는 제거된다',
     f'"{DECODED}"', False, lambda k: k == DECODED),
    ("빈 값은 빈 값으로",
     "", False, lambda k: k == ""),
    ("64자 hex 형태 키도 그대로",
     "0bc260c45e1ed28f789bc43c47239059", False,
     lambda k: k == "0bc260c45e1ed28f789bc43c47239059"),
]


def main() -> int:
    fails = 0
    for desc, raw, want_decoded, check in CASES:
        key, decoded = normalize_service_key(raw)
        ok = (decoded == want_decoded) and check(key)
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"      decoded={decoded}(기대 {want_decoded}) key={key!r}")

    # 왕복: Encoding 키를 디코딩하면 Decoding 키와 같아야 한다
    from urllib.parse import quote
    rt, _ = normalize_service_key(quote(DECODED, safe=""))
    ok = rt == DECODED
    fails += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  인코딩→디코딩 왕복이 원본과 일치한다")

    print(f"\n실패 {fails}건")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
