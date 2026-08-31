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
from datetime import datetime

from .timeutil import now_kst

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
    # '정원' 단독은 TO 를 뜻해 쓸 수 없지만(TIER_C 주석 참고), 아래처럼 조직·업무
    # 단위로 붙으면 명확하다. 실측: 송파 '정원지원센터 유지관리 기간제근로자'가
    # '정원'을 뺀 탓에 걸리지 않았다.
    "정원지원센터", "정원센터", "정원관리", "정원유지", "정원조성", "정원도시",
    # '나무' 단독은 수종·시설 고유명사에 걸려 못 쓰지만(TIER_C 주석) 아래
    # 복합어는 명확하다.
    "나무심기", "나무관리", "나무진료", "나무병원", "가지치기", "전정작업",
    "수목전정", "벌목", "수목식재", "식재공사",
]

# ── TIER C: 부서·조직명 위주. 노이즈가 많아 '확인 필요' 섹션으로 분리. ────
#  C는 제목·부서에서만 본다(CONTENT_TIERS 참고). 본문까지 허용하면 무너진다.
#  '정원' 제외: 채용공고 본문·제목에 '정원 5명'(TO) 으로 상시 등장해 사실상
#  모든 채용공고가 걸린다 — 실측으로 확인. 대신 '정원사'를 TIER_B에 둔다.
#  '나무' 단독 제외: 수종·시설 고유명사에 광범위하게 걸린다(느티나무쉼터,
#  벚나무길, 은행나무 등). 3주 운영에서 '나무'로 걸린 건 전부 오탐이었고,
#  정작 중요한 '나무의사'·'나무병원'은 TIER_A 에 있어 영향이 없다.
#  의미 있는 복합어는 TIER_B 로 옮겼다.
TIER_C = [
    "조경", "녹지", "산림", "수목", "공원관리", "공원운영",
    "식생", "숲",
]

# ── 노이즈 힌트: 등급을 한 단계 낮춘다. 절대 버리지 않는다. ───────────────
#  '정원외'는 '정원 외 직원'(TO 초과)이라 조경과 무관 — 실측으로 확인된 함정.
NEG_HINT = [
    "정원외", "정원 외", "정원내", "결원보충 정원",
    # '꿈의숲'처럼 공원 고유명사가 '숲'에 걸리는데 실제 사업은 무관한 경우가 많다.
    # 실측: '북서울꿈의숲 임시화장실 설치 및 운영 용역'
    "화장실", "폐기물", "포장공사", "청소용역", "보험", "차량", "주차장",
    # 사업 성격이 무관한 유형 (실측: '잠원느티나무쉼터 수탁법인 공개모집')
    # ⚠️ 박물관·도서관·체육관 같은 **시설명은 넣지 않는다** — 그 시설의
    #    녹지관리원 채용이 실제로 있다('서울역사박물관 ... (녹지관리원)').
    "쉼터", "수탁법인", "위탁운영", "복합시설", "음악회", "축제",
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

# ── 결과·경과 공고: 이미 끝난 절차라 지원할 수 없다 ────────────────────
#  실측으로 이 유형이 알림의 상당수를 차지했다. '최종합격자 공고',
#  '서류전형 합격자 및 면접시험 일정' 같은 건 볼 가치가 없다.
#  강등이 아니라 아예 제외한다 — 상세·첨부 조회도 건너뛰어 실행 시간이 줄어든다.
RESULT_NOTICE = re.compile(
    r"합격자|합격\s*발표|불합격|최종\s*합격|합격여부"
    r"|서류\s*전형\s*결과|심사\s*결과|선정\s*결과|전형\s*결과|채용\s*결과"
    r"|면접\s*(시험|심사)\s*(일정|계획|안내|대상)"
    r"|응시\s*번호|필기시험\s*장소"
    # 취소는 '공고 취소'와 '취소공고' 두 어순으로 모두 쓰인다
    r"|(?:채용|모집|공고|입찰|용역|시험)\s*취소|취소\s*(?:공고|알림)"
)

# 각종 위원 모집·위촉. 통상은 채용이 아니지만 **아직 모집 중**이므로
# 결과공고와 달리 무조건 버리면 안 된다.
#   버려야 할 것 : '제25회 서울억새축제 대행 용역 제안서 평가위원 모집'
#                  (공원여가과 부서 안전망에만 걸린 무관 건 — 실측 오탐)
#   남겨야 할 것 : '수목진료 심의위원 위촉' 처럼 나무의사에게 실제 기회인 건
# → A티어(나무의사·수목치료기술자 등 모호하지 않은 용어)가 있을 때만 통과시킨다.
COMMITTEE_NOTICE = re.compile(
    r"(?:심사|평가|심의|자문|선정|평가심의)\s*위원"
    r"|위원\s*(?:모집|위촉|공모|추천|선정)"
)


def is_result_notice(title: str) -> bool:
    """이미 끝난 절차인가. A티어가 있어도 지원할 수 없으므로 무조건 제외한다."""
    return bool(RESULT_NOTICE.search(title or ""))


def is_committee_notice(title: str) -> bool:
    """위원 모집·위촉인가. A티어 근거가 없을 때만 버린다."""
    return bool(COMMITTEE_NOTICE.search(title or ""))


def is_actionable(title: str, tier: str | None) -> bool:
    """등급 판정까지 마친 뒤의 최종 관문 — 알림을 보낼 가치가 있는가.

    is_result_notice 는 성능을 위해 수집 직후(상세 조회 전)에도 한 번 적용하지만,
    규칙의 authoritative 위치는 여기다.
    """
    if not tier:
        return False
    if is_result_notice(title):
        return False                       # 이미 끝난 절차
    if is_committee_notice(title) and tier != "A":
        return False                       # 위원 위촉은 확실한 근거가 있을 때만
    return True


def is_closed(due: str, now: datetime | None = None) -> bool:
    """마감이 이미 지났는가.

    마감을 모르면 버리지 않는다(모르는 것을 놓치는 것보다 낫다).
    날짜만 있으면 그 날 하루는 살아있는 것으로 본다.
    시각까지 있으면 시각으로 비교한다 — 나라장터 소액 수의시담은 공고 당일
    오전에 마감되는 일이 흔해서(실측: 07:59 공고 → 10:00 마감) 날짜만으로는
    이미 끝난 건을 걸러내지 못한다.

    ⚠️ 비교 기준은 반드시 KST 다. 공고의 시각 표기가 KST 인데 Actions 러너는
    UTC 로 돌아서 `datetime.now()` 를 쓰면 9시간 이르게 판단한다
    (실측: 마감 10:00 건이 12:31 KST 실행에서 유효로 잘못 판정돼 발송됐다).
    """
    due = (due or "").strip()
    if not due:
        return False
    now = now or now_kst()
    try:
        if len(due) > 10:                      # 'YYYY-MM-DD HH:MM'
            return datetime.strptime(due[:16], "%Y-%m-%d %H:%M") < now
        return datetime.strptime(due, "%Y-%m-%d").date() < now.date()
    except ValueError:
        return False


_TIERS = (("A", TIER_A), ("B", TIER_B), ("C", TIER_C))
_TIER_ORDER = {"A": 0, "B": 1, "C": 2, None: 9}

# 본문·첨부에서 나온 매칭을 어떤 등급으로 인정할지.
#
#   A → A   : '나무의사', '수목치료기술자' 등은 모호하지 않다. 첨부에서 발견되면
#             그것이 곧 자격 요건이므로 확정 등급으로 올린다. 첨부 파싱을 하는
#             이유가 바로 이것이다.
#   B, C → 무시
#
# B 를 본문·첨부에서 인정하지 않는 이유(실측 오탐 3건):
#   · '해충'  ← 공통 제출서류 양식의 '자격증 종류 목록'에 상투적으로 들어 있다
#              (중구 SNS홍보 채용이 유력으로 잘못 올라왔다)
#   · '정원지원센터' ← 상세 페이지에 함께 렌더링되는 **다른 공고 링크** 에 있었다
#              (송파구 치위생사·도서보조 채용이 확인필요로 잘못 올라왔다)
# 한 단계 강등(B→C)으로는 부족했다. 발송 자체를 막아야 한다.
# B 본문 매칭이 준 이득은 없었다 — 유일한 사례(금천 '임업')는 제목에 이미 B가
# 있어 등급이 바뀌지 않았다.
#
# 제목·부서에서 나온 매칭은 이 표를 거치지 않고 원래 등급을 유지한다.
CONTENT_TIER_MAP = {"A": "A"}


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
    def content_scan(text: str) -> dict[str, list[str]]:
        """본문·첨부 매칭을 CONTENT_TIER_MAP 으로 재등급한다."""
        out: dict[str, list[str]] = {}
        for tier, words in _scan(text).items():
            mapped = CONTENT_TIER_MAP.get(tier)
            if mapped:
                out.setdefault(mapped, []).extend(words)
        return out

    m = Match()
    per_field = {
        "title": _scan(title),
        "body": content_scan(body),
        "attach": content_scan(attach),
    }

    best: str | None = None
    for field_name, found in per_field.items():
        for tier in found:
            if _TIER_ORDER[tier] < _TIER_ORDER[best]:
                best = tier

    if best is None:
        # 안전망: 녹지 소관 부서의 채용·모집 공고
        # 부서 안전망에서 '용역/위탁'은 뺀다. 공원여가센터는 음악회·화장실·
        # 청소 등 온갖 용역을 발주하므로 부서만으로 통과시키면 노이즈가 된다
        # (실측 오탐: '서서울호수공원 수변음악회 기획 및 운영 용역').
        # 수목 관련 용역은 제목에 키워드가 있으므로 이 안전망 없이도 걸린다.
        is_job = re.search(r"채용|모집|선발|구인", title or "")
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
