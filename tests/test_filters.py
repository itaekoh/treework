"""필터 등급 규칙 회귀 테스트.

모든 사례는 실제 공고에서 나왔다. 키워드 목록이나 등급 규칙을 손볼 때
이 테스트가 깨지면 과거에 고쳤던 오탐/누락이 되살아난 것이다.

    python tests/test_filters.py
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from treework import filters  # noqa: E402

# 공통 제출서류 양식에 상투적으로 붙는 자격증 종류 목록 (실제 형태)
BOILERPLATE = ("응시자격 제출서류 목록 자격증 사본 ※ 자격 : 산림, 조경, 종자, "
               "임산가공, 임업종묘, 화훼장식, 농화학, 정보처리, 사무자동화")
# 실제로 나무의사를 요구하는 공고문 본문 형태
REAL_REQ = ("응시자격 - 「산림보호법」에 따른 나무의사 자격을 소지한 사람 "
            "또는 수목치료기술자 자격 소지자")

# (설명, 제목, 본문, 첨부, 부서, 기대등급)
CASES: list[tuple[str, str, str, str, str, str | None]] = [
    ("첨부의 자격증 목록만 걸린 홍보직 채용은 확인필요로 내려가야 한다",
     "서울특별시 중구 임기제공무원 채용시험 최종합격자 공고(SNS구정홍보)",
     "", BOILERPLATE, "홍보담당관", "C"),

    ("제목에 '공원녹지'가 있으면 유력",
     "서울특별시 금천구 시간선택제 임기제공무원(라급,공원녹지분야) 경력경쟁임용시험",
     "", BOILERPLATE, "공원녹지과", "B"),

    ("첨부에 '나무의사'가 있으면 제목이 무관해도 확정으로 승급한다 (핵심 규칙)",
     "○○구 기간제근로자 채용 공고",
     "", REAL_REQ, "", "A"),

    ("제목에 '산림재난' 같은 A티어가 있으면 확정",
     "서울특별시 노원구 시간선택제 임기제공무원(산림재난 대응단 운용 분야) 채용 계획 공고",
     "", "", "푸른도시과", "A"),

    ("키워드가 없어도 녹지 소관 부서의 채용은 확인필요로 잡는다 (안전망)",
     "2026년 기간제근로자 채용 공고", "", "", "푸른도시과", "C"),

    ("무관한 공고는 걸리지 않는다",
     "지방세 체납자 명단 공시송달 공고", "", "", "세무과", None),

    ("공원 고유명사('꿈의숲')에 걸린 무관 용역은 강등한다",
     "북서울꿈의숲 임시화장실 설치 및 운영 용역", "", "", "북부공원여가센터", "C"),

    ("'정원외직원'의 정원은 TO 를 뜻하므로 걸리지 않아야 한다",
     "2026년 제7회 서울연구원 정원외직원 채용 공고", "", "", "조직담당관", None),

    ("'예찰방제단'은 확정",
     "2026년 산림병해충 예찰방제단 모집 공고", "", "", "", "A"),

    ("'녹지관리원'은 유력",
     "서울역사박물관 기간제근로자 채용 공고(녹지관리원)", "", "", "총무과", "B"),
]


def main() -> int:
    fails = 0
    for desc, title, body, attach, dept, want in CASES:
        m = filters.classify(title, body, attach, dept)
        ok = m.tier == want
        fails += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"      기대={want} 실제={m.tier} hits={m.hits[:6]}")
    total = len(CASES)
    print(f"\n{total - fails}/{total} 통과")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
