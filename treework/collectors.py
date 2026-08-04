"""소스별 수집기.

각 함수는 Posting 리스트를 돌려주고, 실패는 예외로 올린다. 예외를 함수 안에서
삼키면 '조용히 0건'이 되어 구조 변경을 영원히 모르게 된다 — 이 시스템에서 가장
위험한 실패 모드이므로 반드시 위로 전파한다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .attach import extract
from .fetcher import Fetcher

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
JS_ARG_RE = re.compile(r"""[('"\s,]\s*(['"]?)([0-9A-Za-z_\-]{2,40})\1\s*[,)]""")


@dataclass
class Posting:
    source_id: str
    org: str
    title: str
    link: str = ""
    dept: str = ""
    reg_date: str = ""
    due_date: str = ""
    detail_id: str = ""
    body: str = ""
    attach_text: str = ""
    attach_names: list[str] = field(default_factory=list)
    attach_truncated: bool = False
    link_is_board: bool = False
    amount: str = ""            # 용역 입찰의 추정가격 등 규모 정보
    # 필터 결과
    tier: str | None = None
    hits: list[str] = field(default_factory=list)
    demoted: bool = False


def _norm_date(text: str) -> str:
    m = DATE_RE.search(text or "")
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _all_dates(text: str) -> list[str]:
    out = []
    for m in DATE_RE.finditer(text or ""):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append(f"{y:04d}-{mo:02d}-{d:02d}")
    return out


def _clean(s: str) -> str:
    s = " ".join((s or "").split())
    return re.sub(r"\s*(NEW|new|New|신규|Hot|HOT)\s*$", "", s).strip()


def _norm_datetime(text: str) -> str:
    """'2026-08-04 10:00:00' → '2026-08-04 10:00'.

    용역 입찰은 소액 수의시담이면 공고 당일 오전에 마감되는 경우가 흔하다
    (실측: 932만원 감리용역이 07:59 공고 → 10:00 마감). 날짜만 보여주면
    이미 지난 건인지 알 수 없어 시각까지 남긴다.
    """
    d = _norm_date(text)
    if not d:
        return ""
    m = re.search(r"(\d{1,2}):(\d{2})", text or "")
    return f"{d} {int(m.group(1)):02d}:{m.group(2)}" if m else d


def _won(v) -> str:
    """'9324493' → '932만원'. 나무의사가 참여할 규모인지 한눈에 보이게."""
    try:
        n = int(float(str(v)))
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}억원"
    if n >= 10_000:
        return f"{n // 10_000:,}만원"
    return f"{n:,}원"


def _within(reg: str, lookback_days: int) -> bool:
    """등록일이 lookback 안이거나 날짜를 모르면 통과."""
    if not reg:
        return True
    try:
        d = datetime.strptime(reg, "%Y-%m-%d").date()
    except ValueError:
        return True
    return d >= date.today() - timedelta(days=lookback_days)


# ═══════════════════════════════════════════════════════════════════
# 범용 테이블 파서
# ═══════════════════════════════════════════════════════════════════
def parse_table(html: str, base_url: str) -> list[dict]:
    """게시판 목록 테이블을 찾아 행을 뽑는다.

    자치구 CMS가 6종 이상으로 갈려 있어 개별 셀렉터를 25개 쓰는 대신 휴리스틱을
    쓴다. 핵심은 '날짜 밀도' 게이팅이다 — 진짜 게시판 목록은 행마다 날짜가 있고,
    부서 목록이나 관련기관 링크 모음에는 날짜가 없다. 날짜 있는 후보가 하나도
    없으면 빈 리스트를 돌려 실패로 처리한다(엉뚱한 링크 모음을 공고로 착각해
    발송하는 것보다 0건 경고가 낫다).
    """
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    cands = []
    for tag in ("table", "ul", "ol"):
        for node in soup.find_all(tag):
            if tag == "table":
                rows = node.select("tbody tr") or node.find_all("tr")
            else:
                rows = node.find_all("li", recursive=False)
            rows = [r for r in rows if r.find("a", href=True)]
            if len(rows) < 3:
                continue
            dated = sum(1 for r in rows if DATE_RE.search(r.get_text(" ", strip=True)))
            cands.append((rows, dated, dated / len(rows)))

    strong = [c for c in cands if c[2] >= 0.5]
    if not strong:
        return []
    rows, _, _ = max(strong, key=lambda c: (len(c[0]), c[1]))

    items: list[dict] = []
    for r in rows:
        anchors = [a for a in r.find_all("a", href=True)
                   if len(a.get_text(" ", strip=True)) > 3]
        if not anchors:
            continue
        a = max(anchors, key=lambda x: len(x.get_text(" ", strip=True)))
        title = _clean(a.get_text(" ", strip=True))
        if not title:
            continue
        row_text = r.get_text(" ", strip=True)
        dates = _all_dates(row_text)
        href = (a.get("href") or "").strip()
        onclick = (a.get("onclick") or "").strip()
        link, detail_id = "", ""
        if href and not href.lower().startswith(("javascript", "#")):
            link = urljoin(base_url, href)
        else:
            args = [g[1] for g in JS_ARG_RE.findall(href + " " + onclick)]
            args = [x for x in args if not x.isalpha() or len(x) > 8]
            detail_id = args[0] if args else ""
        items.append({
            "title": title,
            "link": link,
            "detail_id": detail_id,
            "reg_date": dates[0] if dates else "",
            "due_date": dates[1] if len(dates) > 1 else "",
        })
    return items


# ═══════════════════════════════════════════════════════════════════
# 1. 서울시 일자리포털 공공일자리  (최우선 소스)
# ═══════════════════════════════════════════════════════════════════
def collect_seoul_job_portal(f: Fetcher, cfg: dict, lookback_days: int) -> list[Posting]:
    end = date.today()
    start = end - timedelta(days=max(lookback_days, 14))
    data = {
        "miv_pageNo": "1",
        "miv_pageSize": str(cfg.get("page_size", 200)),
        "page_size_sel": str(cfg.get("page_size", 200)),
        "sidx": "FRST_REG_DT", "sord": "DESC",
        "searchkey": "ttl", "searchtxt": "",
        "searchDateType": "frst_reg_dt",
        "searchDate_stdt": start.isoformat(),
        "searchDate_endt": end.isoformat(),
    }
    r = f.post(cfg["list_url"], data=data,
               headers={"Referer": cfg["referer"],
                        "Content-Type": "application/x-www-form-urlencoded"})
    soup = BeautifulSoup(f.text_of(r), "lxml")
    out: list[Posting] = []
    for tr in soup.select(".boardlist table tbody tr"):
        a = tr.select_one(".title a")
        if not a:
            continue
        tds = tr.find_all("td")
        m = re.search(r"'([0-9A-Fa-f]{16,40})'", a.get("href") or "")
        pid = m.group(1) if m else ""
        out.append(Posting(
            source_id=cfg["id"], org=cfg.get("org", "서울특별시"),
            title=_clean(a.get_text(" ", strip=True)),
            dept=_clean(tds[0].get_text(" ", strip=True)) if tds else "",
            reg_date=_norm_date(tds[2].get_text()) if len(tds) > 2 else "",
            due_date=_norm_date(tds[3].get_text()) if len(tds) > 3 else "",
            detail_id=pid,
            link=cfg["detail_template"].format(id=pid) if pid else cfg["referer"],
        ))
    return out


def enrich_seoul_job_portal(f: Fetcher, p: Posting, cfg: dict) -> None:
    """상세 페이지 + 첨부. 이 소스는 상세요강이 대부분 비어 있고 실제 내용이
    .hwpx 첨부에만 있으므로 첨부 파싱이 사실상 필수다."""
    if not p.detail_id:
        return
    r = f.get(cfg["detail_template"].format(id=p.detail_id))
    soup = BeautifulSoup(f.text_of(r), "lxml")
    main = soup.select_one("#container, #content, .contents") or soup
    p.body = main.get_text("\n", strip=True)[:60000]
    if not cfg.get("parse_attachments", True):
        return
    for a in soup.find_all("a", href=True):
        name = _clean(a.get_text())
        if not re.search(r"\.(hwpx?|pdf|docx?|xlsx?|zip)$", name, re.I):
            continue
        url = urljoin(r.url, a["href"])
        p.attach_names.append(name)
        try:
            fr = f.get(url)
            ex = extract(fr.content, name)
            if ex.text:
                p.attach_text += "\n" + ex.text[:80000]
            if ex.truncated or ex.error:
                p.attach_truncated = True
                if ex.error:
                    log.debug("첨부 추출 실패 %s: %s", name, ex.error)
        except Exception as e:                       # noqa: BLE001
            p.attach_truncated = True
            log.debug("첨부 다운로드 실패 %s: %s", name, e)


# ═══════════════════════════════════════════════════════════════════
# 2. 서울시 고시·공고
# ═══════════════════════════════════════════════════════════════════
def collect_seoul_notice(f: Fetcher, cfg: dict, lookback_days: int) -> list[Posting]:
    out: list[Posting] = []
    for page in range(1, int(cfg.get("pages", 2)) + 1):
        r = f.get(cfg["list_url"], params={
            "cntPerPage": cfg.get("page_size", 50), "curPage": page})
        soup = BeautifulSoup(f.text_of(r), "lxml")
        rows = soup.select("table.sib-lst-type-basic tbody tr")
        if not rows:
            raise RuntimeError("고시공고 목록 테이블을 찾지 못함 (구조 변경 의심)")
        for tr in rows:
            a = tr.select_one("td.sib-lst-type-basic-subject a")
            if not a:
                continue
            tds = tr.find_all("td")
            m = re.search(r"fnTbbsView\('(\d+)'\)", a.get("href") or "")
            ntt = m.group(1) if m else ""
            out.append(Posting(
                source_id=cfg["id"], org=cfg.get("org", "서울특별시"),
                title=_clean(a.get_text(" ", strip=True)),
                dept=_clean(tds[2].get_text()) if len(tds) > 2 else "",
                reg_date=_norm_date(tds[3].get_text()) if len(tds) > 3 else "",
                due_date=_norm_date(tds[4].get_text()) if len(tds) > 4 else "",
                detail_id=ntt,
                link=(f"{cfg['list_url']}?bbsNo={cfg.get('bbs_no', 277)}"
                      f"&nttNo={ntt}") if ntt else cfg["list_url"],
            ))
    return out


def enrich_seoul_notice(f: Fetcher, p: Posting, cfg: dict) -> None:
    """이 게시판은 HWP 본문이 HTML(div#scrabArea)로 변환되어 전문이 실린다."""
    if not p.link:
        return
    r = f.get(p.link)
    soup = BeautifulSoup(f.text_of(r), "lxml")
    body = soup.select_one("#scrabArea") or soup.select_one("#content") or soup
    p.body = body.get_text("\n", strip=True)[:60000]
    for dl in soup.select("div.view-column dl"):
        dt = dl.find("dt")
        dd = dl.find("dd")
        if not (dt and dd):
            continue
        label, val = dt.get_text(strip=True), _clean(dd.get_text(" "))
        if "마감" in label and not p.due_date:
            p.due_date = _norm_date(val)
        elif "담당부서" in label and not p.dept:
            p.dept = val
    p.attach_names = [_clean(a.get_text()) for a in soup.find_all("a", href=True)
                      if re.search(r"getFile", a["href"])]


# ═══════════════════════════════════════════════════════════════════
# 3. 자치구 eMinwon 공통 고시공고 시스템
# ═══════════════════════════════════════════════════════════════════
EMINWON_CODES = "01,02,03,04,05,06,07"


def collect_eminwon(f: Fetcher, cfg: dict, lookback_days: int) -> list[Posting]:
    host = cfg["host"]
    action = f"https://{host}/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do"
    referer = (f"https://{host}/emwp/jsp/ofr/OfrNotAncmtLSub.jsp"
               f"?not_ancmt_se_code={EMINWON_CODES}")
    data = {
        "pageIndex": "1", "jndinm": "OfrNotAncmtEJB", "context": "NTIS",
        "method": "selectListOfrNotAncmt",
        "methodnm": "selectListOfrNotAncmtHomepage",
        "not_ancmt_mgt_no": "", "homepage_pbs_yn": "Y", "subCheck": "Y",
        "ofr_pageSize": str(cfg.get("page_size", 50)),
        "not_ancmt_se_code": EMINWON_CODES,
        "cha_dep_code_nm": "", "not_ancmt_sj": "",
    }
    r = f.post(action, data=data, headers={"Referer": referer})
    soup = BeautifulSoup(f.text_of(r), "lxml")

    best, best_dated = [], -1
    for t in soup.find_all("table"):
        rows = [x for x in (t.select("tbody tr") or t.find_all("tr")) if x.find("a")]
        dated = sum(1 for x in rows if DATE_RE.search(x.get_text(" ", strip=True)))
        if len(rows) >= 3 and dated > best_dated:
            best, best_dated = rows, dated
    if best_dated < 3:
        raise RuntimeError("eMinwon 목록 파싱 실패 (구조 변경 의심)")

    board = cfg.get("board_url") or referer
    out: list[Posting] = []
    for tr in best:
        row_text = tr.get_text(" ", strip=True)
        if not DATE_RE.search(row_text):
            continue                       # 헤더 행 제외
        # 번호 셀도 <a> 라서 첫 앵커를 쓰면 "219" 가 제목이 된다.
        # 가장 긴 앵커 텍스트를 제목으로 본다.
        anchors = [a for a in tr.find_all("a")
                   if len(a.get_text(" ", strip=True)) > 3]
        if not anchors:
            continue
        a = max(anchors, key=lambda x: len(x.get_text(" ", strip=True)))
        title = _clean(a.get_text(" ", strip=True))
        if not title or title.isdigit():
            continue
        tds = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
        # 컬럼: 번호 | 고시공고번호 | 제목 | 담당부서 | 등록일 | 게재기간 | 조회수
        dept = next((t for t in tds
                     if t != title and 2 <= len(t) <= 12
                     and t.endswith(("과", "팀", "관", "소", "국", "실"))), "")
        dates = _all_dates(row_text)
        m = re.search(r"searchDetail\('(\d+)'\)",
                      (a.get("href") or "") + (a.get("onclick") or ""))
        out.append(Posting(
            source_id=cfg["id"], org=cfg.get("org", ""),
            title=title, dept=dept,
            reg_date=dates[0] if dates else "",
            # 게재기간의 끝이 실질 마감일
            due_date=dates[-1] if len(dates) > 1 else "",
            detail_id=m.group(1) if m else "",
            # 상세가 POST 전용이라 공유 링크가 없다 → 게시판으로 보낸다
            link=board, link_is_board=True,
        ))
    return out


# ═══════════════════════════════════════════════════════════════════
# 4. 범용 테이블 소스 (자치구 16곳)
# ═══════════════════════════════════════════════════════════════════
def collect_generic(f: Fetcher, cfg: dict, lookback_days: int) -> list[Posting]:
    r = f.get(cfg["list_url"])
    items = parse_table(f.text_of(r), r.url)
    if not items:
        raise RuntimeError("날짜 있는 목록 테이블을 찾지 못함 (구조 변경/AJAX 의심)")
    use_board = cfg.get("link_mode") == "list"
    out = []
    for it in items:
        link = it["link"]
        is_board = False
        if use_board or not link:
            link, is_board = cfg["list_url"], True
        out.append(Posting(
            source_id=cfg["id"], org=cfg.get("org", ""),
            title=it["title"], link=link, link_is_board=is_board,
            reg_date=it["reg_date"], due_date=it["due_date"],
            detail_id=it["detail_id"],
        ))
    return out


def enrich_generic(f: Fetcher, p: Posting, cfg: dict) -> None:
    """직접 링크가 있을 때만 본문을 받는다."""
    if p.link_is_board or not p.link:
        return
    r = f.get(p.link)
    soup = BeautifulSoup(f.text_of(r), "lxml")
    for t in soup(["script", "style", "nav", "header", "footer"]):
        t.decompose()
    main = (soup.select_one("#content, #contents, .content, .board_view, .bbs_view")
            or soup.body or soup)
    p.body = main.get_text("\n", strip=True)[:60000]
    p.attach_names = [_clean(a.get_text()) for a in soup.find_all("a", href=True)
                      if re.search(r"\.(hwpx?|pdf|docx?|xlsx?|zip)(\?|$)",
                                   a.get_text() + a["href"], re.I)][:12]


# ═══════════════════════════════════════════════════════════════════
# 5. 나라장터 입찰공고 (용역)
#
# 나무의사 실수요는 채용보다 용역 발주로 나오는 경우가 많다.
#   '○○구 생활권 수목 진단 및 진료 용역', '가로수 병해충 예찰방제 용역'
#
# 실측(2026-08-04) — 이 API 는 날짜 기준 조회만 지원한다:
#   · bidNtceNm / srchWord / ntceNm 등 제목 검색 파라미터는 전부 무시된다
#     (같은 기간에 어떤 키워드를 넣어도 totalCount 8937 로 동일)
#   · prtcptLmtRgnCd 등 지역 파라미터도 무시된다
#   · numOfRows 상한은 999 (1000 을 넣으면 기본값 10 으로 떨어진다)
#   · 정렬은 등록일시 오름차순, 전국 용역 평일 하루 약 700건, 서울 비중 약 9%
#   · 엔드포인트는 /1230000/ad/BidPublicInfoService/ 만 유효
#     (구버전 BidPublicInfoService04~06 은 NO_OPENAPI_SERVICE_ERROR)
#
# 따라서 날짜 구간을 전량 페이징해서 받고 지역·키워드는 로컬에서 거른다.
# ═══════════════════════════════════════════════════════════════════
class MissingKey(RuntimeError):
    """API 키 미설정 — 실패가 아니라 '건너뜀'으로 다룬다."""


def collect_g2b_bid(f: Fetcher, cfg: dict, lookback_days: int) -> list[Posting]:
    import os

    key = os.environ.get(cfg.get("key_env", "G2B_SERVICE_KEY"), "").strip()
    if not key:
        raise MissingKey(
            f"{cfg.get('key_env', 'G2B_SERVICE_KEY')} 미설정 — 나라장터 수집 건너뜀")

    url = f"{cfg['base_url'].rstrip('/')}/{cfg['operation']}"
    days = int(cfg.get("lookback_days") or lookback_days)
    end = datetime.now()
    start = end - timedelta(days=max(days, 2))
    region_words = cfg.get("region_keywords") or ["서울"]
    page_size = min(int(cfg.get("page_size", 999)), 999)      # 실측 상한 999
    max_pages = int(cfg.get("max_pages", 15))

    def fetch(page: int) -> dict:
        params = {
            # 공공데이터포털은 '디코딩 키'를 넣고 requests 가 인코딩하게 해야 한다.
            # '인코딩 키'를 넣으면 %2B 가 %252B 로 이중 인코딩돼 인증 실패한다.
            "serviceKey": key,
            "pageNo": str(page),
            "numOfRows": str(page_size),
            "inqryDiv": "1",                       # 1 = 공고게시일시 기준
            "inqryBgnDt": start.strftime("%Y%m%d") + "0000",
            "inqryEndDt": end.strftime("%Y%m%d") + "2359",
            "type": "json",
        }
        # 인증 오류를 403 + 본문 JSON 으로 알려주므로 상태코드로 끊지 않는다
        r = f.get(url, params=params, check_status=False)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                f"HTTP {r.status_code} · JSON 아님 (앞부분: {r.text[:160]})")
        # 인증/한도 오류는 공고 0건과 구분해서 즉시 드러내야 한다
        if "OpenAPI_ServiceResponse" in data:
            hdr = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            raise RuntimeError(f"API 오류: {hdr.get('errMsg')} / "
                               f"{hdr.get('returnAuthMsg')}")
        resp = data.get("response") or {}
        header = resp.get("header") or {}
        code = str(header.get("resultCode", ""))
        if code not in ("", "00", "0"):
            raise RuntimeError(f"API 오류 {code}: {header.get('resultMsg')}")
        return resp.get("body") or {}

    seen_no: set[str] = set()
    out: list[Posting] = []
    total = 0
    pages_read = 0
    for page in range(1, max_pages + 1):
        body = fetch(page)
        pages_read = page
        if page == 1:
            total = int(body.get("totalCount") or 0)
            want = min(-(-total // page_size), max_pages) if total else 0
            log.info("[%s] 전국 용역 %d건 (%s~%s) → %d페이지 조회",
                     cfg["id"], total, start.date(), end.date(), want)
        items = body.get("items") or []
        if isinstance(items, dict):               # 1건일 때 dict 로 오는 경우
            items = items.get("item") or items
        if isinstance(items, dict):
            items = [items]
        if not items:
            break

        for it in items:
            uid = f"{it.get('bidNtceNo')}-{it.get('bidNtceOrd')}"
            if uid in seen_no:
                continue
            org = str(it.get("ntceInsttNm") or "")
            demand = str(it.get("dminsttNm") or "")
            # 지역 파라미터가 무시되므로 기관명으로 서울 발주만 남긴다
            if not any(w in org or w in demand for w in region_words):
                continue
            seen_no.add(uid)
            out.append(Posting(
                source_id=cfg["id"],
                org=demand or org or "나라장터",
                # 발주기관명을 부서 자리에 넣어 '녹지 소관' 안전망이 걸리게 한다
                # (예: 북부공원여가센터, 푸른도시여가국)
                dept=org,
                title=_clean(str(it.get("bidNtceNm") or "")),
                reg_date=_norm_date(str(it.get("bidNtceDt") or "")),
                # 마감은 시각까지 (당일 마감 건이 흔하다)
                due_date=_norm_datetime(str(it.get("bidClseDt") or "")
                                        or str(it.get("opengDt") or "")),
                amount=_won(it.get("presmptPrce") or it.get("asignBdgtAmt")),
                link=str(it.get("bidNtceDtlUrl") or "") or cfg.get("board_url", ""),
                detail_id=uid,
                # 공고명이 곧 사업 내용이라 별도 본문 없이도 판정이 가능하다
                body=" ".join(filter(None, [
                    str(it.get("ntceKindNm") or ""),
                    str(it.get("bidprcPsblIndstrytyNm") or ""),
                    str(it.get("rgnLmtBidLocplcJdgmBssNm") or ""),
                ])),
            ))

        if total and page * page_size >= total:
            break

    # 상한에 걸려 뒷부분을 못 봤으면 조용히 넘기지 않는다.
    # 커버리지가 잘렸는데 정상으로 보고되면 '조용한 누락'이 된다.
    if total > pages_read * page_size:
        log.warning("[%s] 페이지 상한 도달 — 전체 %d건 중 %d건만 확인. "
                    "lookback_days 를 줄이거나 max_pages 를 늘리세요.",
                    cfg["id"], total, pages_read * page_size)

    log.info("[%s] %d페이지에서 서울 발주 %d건 추출", cfg["id"], pages_read, len(out))
    return out


def enrich_eminwon(f: Fetcher, p: Posting, cfg: dict) -> None:
    """eMinwon 상세를 POST로 받아 전체 제목과 본문을 채운다.

    목록의 제목은 일정 길이에서 잘린다(예: '...(산림재난 대응단 운용 분야) 채...').
    잘린 뒷부분에 키워드가 있으면 제목만으로는 놓치므로 상세를 받아야 한다.
    상세는 POST 전용이라 공유 링크는 만들 수 없다(link 은 게시판 유지).
    """
    if not p.detail_id:
        return
    host = cfg["host"]
    action = f"https://{host}/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do"
    referer = (f"https://{host}/emwp/jsp/ofr/OfrNotAncmtLSub.jsp"
               f"?not_ancmt_se_code={EMINWON_CODES}")
    r = f.post(action, data={
        "pageIndex": "1", "jndinm": "OfrNotAncmtEJB", "context": "NTIS",
        "method": "selectOfrNotAncmt", "methodnm": "selectOfrNotAncmtRegst",
        "not_ancmt_mgt_no": p.detail_id, "homepage_pbs_yn": "Y",
        "subCheck": "Y", "ofr_pageSize": "10",
        "not_ancmt_se_code": EMINWON_CODES,
        "cha_dep_code_nm": "", "not_ancmt_sj": "",
    }, headers={"Referer": referer})
    soup = BeautifulSoup(f.text_of(r), "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    p.body = text[:60000]
    # 잘리지 않은 전체 제목으로 교체 (목록 제목이 접두사인 가장 긴 줄)
    stem = p.title.rstrip(". ")[:18]
    if stem:
        full = [ln for ln in (l.strip() for l in text.splitlines())
                if stem in ln and len(ln) > len(p.title)]
        if full:
            p.title = _clean(max(full, key=len))[:300]
    p.attach_names = [_clean(a.get_text()) for a in soup.find_all("a", href=True)
                      if re.search(r"\.(hwpx?|pdf|docx?|xlsx?|zip)",
                                   a.get_text() + a["href"], re.I)][:12]


COLLECTORS = {
    "seoul_job_portal": (collect_seoul_job_portal, enrich_seoul_job_portal),
    "seoul_notice": (collect_seoul_notice, enrich_seoul_notice),
    "eminwon": (collect_eminwon, enrich_eminwon),
    "generic_table": (collect_generic, enrich_generic),
    "g2b_bid": (collect_g2b_bid, None),
}
