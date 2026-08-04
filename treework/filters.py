"""키워드 필터.

설계 근거 — 서울시 일자리포털 공공일자리 4년치(3,826건) 제목 전수 검색 실측(2026-08-04):

    '나무의사'        0건        '녹지'    36건
    '수목치료기술자'   0건        '공원녹지' 18건
    '수목진료'        0건        '수목'    17건
    '예찰'           0건        '조경'     8건
    '가로수'          0건        '산림'     6건

즉 '나무의사'만으로 거르면 영구히 0건이 나온다. 실제 공고 제목은 이런 식이다.

    [북부공원여가센터] 2024년 기간제노동자(산림병해충방제) 채용 공고
    서울역사박물관 기간제근로자 채용 공고(녹지관리원)
    2026년 서울대공원 조경분야 기간제근로자 결원보충 채용 공고
    노원구 시간선택제임기제공무원 다급(산림재난 대응단 운용 분야) 채용계획 공고

'나무의사'/'수목치료기술자'는 직무명이 아니라 자격 요건이고, 대체로 HWP 첨부
안에만 등장한다. 그래서 제목은 넓게 걷고(TIER_B/C) 본문·첨부에서 TIER_A가
잡히면 등급을 올린다.

누락 방지가 목표이므로 과잘림(놓침)보다 과보고(노이즈)를 택한다.
NEG_HINT는 후보를 버리지 않고 등급만 낮춘다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── TIER A: 수목진료 법정·전문 용어. 걸리면 사실상 확정. ──────────────────
TIER_A = [
    "나무의사", "수목치료기술자", "수목진료", "수목진단", "수목치료",
    "나무병원", "수목외과", "수목보호", "생활권수목", "생활권 수목",
    "산림병해충", "병해충방제", "병해충 방제", "예찰방제", "예찰·방제",
    "예찰 방제", "소나무재선충", "재선충", "참나무시들음병",
    "수목피해", "수목관리원", "산림재난",
]

# ── TIER B: 실제로 쓰이는 직무·사업명. 나무의사가 투입되는 자리. ──────────
TIER_B = [
    "녹지관리", "녹지관리원", "녹지직", "조경분야", "조경직", "조경기사",
    "가로수", "수목관리", "산림관리", "산림치유", "산림보호", "임업",
    "방제단", "예찰", "병해충", "해충", "방제",
    "수목원", "식물병원", "식물보호", "산림기술", "숲가꾸기",
    "공원녹지", "푸른도시", "공원여가센터", "정원사", "조경관리",
]

# ── TIER C: 부서·조직명 위주. 노이즈가 많아 '확인 필요' 섹션으로 분리. ────
#  C는 제목·부서에서만 본다(CONTENT_TIERS 참고). 본문까지 허용하면 무너진다.
#  '정원' 제외: 채용공고 본문·제목에 '정원 5명'(TO) 으로 상시 등장해 사실상
#  모든 채용공고가 걸린다 — 실측으로 확인. 대신 '정원사'를 TIER_B에 둔다.
TIER_C = [
    "조경", "녹지", "산림", "수목", "공원관리", "공원운영",
    "나무", "식생", "숲",
]

# ── 노이즈 힌트: 등급을 한 단계 낮춘다. 절대 버리지 않는다. ───────────────
#  '정원외'는 '정원 외 직원'(TO 초과)이라 조경과 무관 — 실측으로 확인된 함정.
NEG_HINT = [
    "정원외", "정원 외", "정원내", "결원보충 정원",
    "식물전문도서관", "기프트샵", "매표", "주차", "청소", "미화", "경비",
    "수영장", "캠핑장", "안전요원", "조리원", "보육교사", "요양보호사",
    "간호사", "사회복지사", "생활복지사", "사서", "통역", "상담사",
    "공시송달", "과태료", "행정처분", "체납", "압류", "거주불명",
    "담배소매인", "영업신고", "인가", "택시", "이륜자동차",
]

# ── 부서명 신호 ─────────────────────────────────────────────────────────
#  나무의사가 투입되는 공고는 사실상 예외 없이 이 부서들에서 나온다.
#  실측: 노원구 '산림재난 대응단' → 푸른도시과 / 서울시 '산림병해충방제' →
#  조경지원과(북부공원여가센터) / '녹지관리원' → 총무과(박물관·과학관).
#  제목에 키워드가 없어도 이 부서 공고는 최소 '확인필요'로 올려 누락을 막는다.
DEPT_HINT = [
    "푸른도시", "공원녹지", "조경", "녹지", "산림", "공원여가", "공원운영",
    "정원도시", "생태", "자연", "수목", "임업",
]

_TIERS = (("A", TIER_A), ("B", TIER_B), ("C", TIER_C))
_TIER_ORDER = {"A": 0, "B": 1, "C": 2, None: 9}

# 본문·첨부에서 인정하는 티어. C를 본문에까지 허용하면 '조경/녹지/나무'가
# 페이지 메뉴·푸터·안내문에 널려 있어 거의 모든 공고가 걸린다(실측 확인).
# A/B만 본문 근거로 인정하고, C는 제목·부서 전용으로 둔다.
CONTENT_TIERS = {"A", "B"}


def _norm(s: str) -> str:
    """공백·중점 변형을 흡수해 '산림 병해충'도 '산림병해충'과 같이 취급."""
    return re.sub(r"[\s·ㆍ・/\-_()\[\]]+", "", s or "")


@dataclass
class Match:
    tier: str | None = None
    hits: list[str] = field(default_factory=list)      # 매칭된 키워드
    where: list[str] = field(default_factory=list)     # title / body / attach
    demoted: bool = False
    neg: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.tier is not None


def _scan(text: str) -> dict[str, list[str]]:
    """정규화 텍스트에서 티어별 매칭 키워드를 찾는다."""
    n = _norm(text)
    found: dict[str, list[str]] = {}
    for tier, words in _TIERS:
        for w in words:
            if _norm(w) in n:
                found.setdefault(tier, []).append(w)
    return found


def _demote(tier: str) -> str:
    return {"A": "B", "B": "C", "C": "C"}[tier]


def dept_is_green(dept: str) -> bool:
    """담당부서가 수목·녹지 소관인지."""
    n = _norm(dept)
    return bool(n) and any(_norm(w) in n for w in DEPT_HINT)


def classify(title: str, body: str = "", attach: str = "",
             dept: str = "") -> Match:
    """제목/본문/첨부/부서를 보고 등급을 정한다.

    - 제목에서 A가 걸리면 그대로 A.
    - 제목이 B/C인데 본문·첨부에서 A가 걸리면 A로 승급 (나무의사 자격 요건이
      첨부에만 있는 실제 케이스를 잡기 위한 핵심 규칙).
    - 키워드가 전혀 없어도 담당부서가 수목·녹지 소관이고 채용/모집 공고면
      '확인필요'로 올린다. 목록 제목이 잘려 키워드가 안 보이는 경우와,
      직무명에 수목 관련 어휘가 아예 없는 경우를 잡는 안전망이다.
    - 노이즈 힌트가 제목에 있으면 한 단계 강등. 단 A는 본문/첨부 근거가 있으면
      강등하지 않는다.
    """
    m = Match()
    per_field = {
        "title": _scan(title),
        # 본문·첨부는 A/B만 인정 (CONTENT_TIERS)
        "body": {t: w for t, w in _scan(body).items() if t in CONTENT_TIERS},
        "attach": {t: w for t, w in _scan(attach).items() if t in CONTENT_TIERS},
    }

    best: str | None = None
    for field_name, found in per_field.items():
        for tier in found:
            if _TIER_ORDER[tier] < _TIER_ORDER[best]:
                best = tier

    if best is None:
        # 안전망: 녹지 소관 부서의 채용·모집 공고
        is_job = re.search(r"채용|모집|선발|구인|용역|위탁", title or "")
        if dept_is_green(dept) and is_job:
            m.tier = "C"
            m.hits = [f"부서:{dept}"]
            m.where = ["dept"]
            return m
        return m

    seen: set[str] = set()
    for field_name, found in per_field.items():
        if not found:
            continue
        m.where.append(field_name)
        for tier, words in sorted(found.items()):
            for w in words:
                label = f"{w}({tier})"
                if label not in seen:
                    seen.add(label)
                    m.hits.append(label)

    n_title = _norm(title)
    m.neg = [w for w in NEG_HINT if _norm(w) in n_title]

    a_in_content = "A" in per_field["body"] or "A" in per_field["attach"]
    if m.neg and not (best == "A" and a_in_content):
        best = _demote(best)
        m.demoted = True

    m.tier = best
    return m


def title_prefilter(title: str) -> bool:
    """상세 페이지를 받아올지 결정하는 값싼 1차 관문.

    어떤 티어라도 제목에 걸리면 통과. 통과 못한 공고는 상세를 받지 않는다
    (전량 상세 조회는 사이트 부하가 크고 차단 위험이 있다).
    """
    return _scan(title) != {}
