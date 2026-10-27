"""텔레그램 발송.

parse_mode 는 HTML을 쓴다. 공고 제목에 '[', '(', '-', '_' 가 대량으로 들어
있어서 Markdown/MarkdownV2 는 이스케이프 실수 한 번에 400 Bad Request 로
메시지가 통째로 유실된다. HTML은 &, <, > 세 글자만 막으면 안전하다.

메시지는 4096자 제한에 맞춰 공고 단위로 쪼갠다(공고가 잘리지 않게).
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

import requests

from treework import ai
from treework.timeutil import now_kst

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"

LIMIT = 3800          # 4096에서 여유를 둔다
TIER_BADGE = {"A": "🔴 확정", "B": "🟡 유력", "C": "⚪ 확인필요"}
TIER_DOT = {"A": "🔴", "B": "🟡", "C": "⚪"}


def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def to_plain(text: str) -> str:
    """HTML 서식을 벗긴다. 링크 주소는 살린다.

    <a href="X">Y</a> 를 그냥 지우면 주소가 사라져 알림이 쓸모없어진다.
    이 알림의 핵심은 '어디로 가면 되는가' 다.
    """
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"\2 \1", text,
                  flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _deadline(due: str, now: datetime | None = None) -> str:
    """마감을 남은 날짜로 보여 준다.

    '2026-10-30' 만 적혀 있으면 급한 건인지 알 수 없다. 자치구 기간제 공고는
    접수기간이 3~5일로 짧아서, 알림을 열었을 때 가장 먼저 보여야 하는 것이
    '며칠 남았나' 다.
    """
    due = (due or "").strip()
    if not due:
        return "🗓 마감 미표기"
    now = now or now_kst()
    try:
        if len(due) > 10:                       # 'YYYY-MM-DD HH:MM'
            dt = datetime.strptime(due[:16], "%Y-%m-%d %H:%M")
            stamp = f"{dt:%m/%d} {dt:%H:%M}"
        else:
            dt = datetime.strptime(due, "%Y-%m-%d")
            stamp = f"{dt:%m/%d}"
    except ValueError:
        return f"🗓 {esc(due)} 마감"
    left = (dt.date() - now.date()).days
    if left < 0:
        return f"🗓 {stamp} 마감 (지남)"
    if left == 0:
        return f"🔥 <b>오늘 마감</b> · {stamp}"
    if left == 1:
        return f"🔥 <b>내일 마감</b> · {stamp}"
    if left <= 3:
        return f"⏳ <b>D-{left}</b> · {stamp} 마감"
    return f"🗓 {stamp} 마감 · D-{left}"


def format_posting(p: dict) -> str:
    """공고 1건을 블록으로 만든다.

    읽는 순서를 눈이 움직이는 순서에 맞춘다.
        제목 → 누가 → 언제까지·얼마 → 왜 걸렸나 → AI 판단 → 링크
    제목이 첫 줄 맨 앞에 와야 목록을 훑을 수 있다. 등급 표시는 점 하나로
    줄였다 — 섹션 머리에 이미 이름이 있고, 긴 이름표가 제목을 밀어낸다.
    """
    tier = p.get("tier") or "C"
    out = [f"{TIER_DOT.get(tier, '⚪')} <b>{esc(p['title'])}</b>"]

    # ── 누가 ──
    org, dept = (p.get("org") or "").strip(), (p.get("dept") or "").strip()
    # 나라장터는 공고기관과 수요기관이 같은 경우가 많아 그대로 두면 같은 이름이
    # 두 번 찍힌다. 부서가 기관명에 이미 포함돼 있으면 생략한다.
    if dept and (dept == org or dept in org or org in dept):
        dept = ""
    who = " · ".join(x for x in (esc(org or "-"), esc(dept)) if x)
    region = (p.get("region") or "").strip()
    if region:                       # 전국 수집이라 지역이 보여야 추려낼 수 있다
        who += f" · 📍{esc(region)}"
    out.append(f"🏢 {who}")

    # ── 언제까지 · 얼마 ──
    when = _deadline(p.get("due_date"))
    if p.get("amount"):
        when += f" · 💰 {esc(p['amount'])}"
    out.append(when)

    # ── 왜 걸렸나 ──
    #  '재선충(A)' 의 등급 꼬리표는 떼어 낸다. 등급은 이미 점으로 보인다.
    hits = [re.sub(r"\([ABC]\)$", "", h) for h in (p.get("hits") or [])]
    why = " · ".join(dict.fromkeys(hits))[:80]
    if p.get("attach_names"):
        tag = f"📎{len(p['attach_names'])}"
        why = f"{why} · {tag}" if why else tag
    if why:
        out.append(f"🔎 {esc(why)}")

    # ── AI 판단 ──
    if p.get("ai_fit") is not None:
        out.append(f"🤖 <b>{p['ai_fit']}</b>/5"
                   + (f" · {esc(p.get('ai_why') or '')}" if p.get("ai_why") else ""))

    # ── 믿어도 되는가 ──
    #  판정을 흔드는 사정만 남긴다. 단순 정보는 위에서 이미 말했다.
    warn = []
    if p.get("attach_truncated"):
        warn.append("첨부 일부만 판독")
    if p.get("demoted"):
        warn.append("노이즈 키워드로 등급 하향")
    if p.get("link_is_board"):
        warn.append("직접 링크 없음 — 게시판에서 검색")
    if warn:
        out.append(f"⚠️ <i>{esc(' · '.join(warn))}</i>")

    # ── 링크 ──
    #  나라장터 상세는 WebSquare SPA 라 임베디드 웹뷰에서 실패할 수 있어
    #  공고문 직접 다운로드를 나란히 준다. 공고번호는 <code> 로 둬서
    #  링크가 안 열릴 때 길게 눌러 복사해 직접 검색할 수 있게 한다.
    link, doc = p.get("link") or "", p.get("doc_url") or ""
    nav = []
    if link:
        nav.append(f'<a href="{esc(link)}">공고 상세</a>')
    if doc:
        nav.append(f'<a href="{esc(doc)}">📄 공고문</a>')
    if nav:
        out.append("🔗 " + " · ".join(nav))
    if p.get("ref_no"):
        out.append(f"🔢 <code>{esc(p['ref_no'])}</code>")
    return "\n".join(out)



def build_messages(postings: list[dict], health: dict) -> list[str]:
    """발송할 메시지 목록. 신규가 없어도 문제가 있으면 헬스 메시지를 만든다."""
    msgs: list[str] = []

    # 업역별로 메시지를 나눈다. 수목과 IT 는 판단 기준도 후속 행동도 달라
    # 한 덩어리로 묶으면 둘 다 흐릿해진다.
    GROUPS = (("tree", "\U0001F333", "수목·나무의사 공고"),
              ("it", "\U0001F4BB", "나라장터 IT용역"))
    for prof_name, icon, label in GROUPS:
        group = [p for p in postings if (p.get("profile") or "tree") == prof_name]
        if not group:
            continue

        # AI 가 낮게 본 건은 **버리지 않는다**. 본문에서 빼고 끝에 제목만
        # 한 줄씩 모은다 — 읽는 부담은 줄이되 누락은 만들지 않는다. AI 가
        # 꺼져 있거나 실패했으면 is_demoted 가 늘 False 라 전부 본문에 남는다.
        main_items = [p for p in group if not ai.is_demoted(p)]
        aside = [p for p in group if ai.is_demoted(p)]

        by_tier: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
        for p in main_items:
            by_tier.setdefault(p.get("tier") or "C", []).append(p)

        # 머리글은 두 줄로 고정한다. 무엇이 / 몇 건이 / 어떤 비중인지.
        tally = " · ".join(f"{TIER_DOT[t]} {len(by_tier[t])}"
                           for t in ("A", "B", "C") if by_tier[t])
        if aside:
            tally += f" · 🔅 {len(aside)}"
        head = (f"{icon} <b>{label}</b> · 신규 {len(group)}건\n"
                f"{tally}  <i>{now_kst():%m/%d %H:%M}</i>")
        buf = [head]
        size = len(head)

        def flush():
            # 4096자 제한에 걸리면 공고 단위로 끊는다(공고가 잘리지 않게).
            nonlocal buf, size
            if buf:
                msgs.append("\n".join(buf))
            buf, size = [], 0

        def put(chunk: str):
            nonlocal size
            if size + len(chunk) > LIMIT:
                flush()
            buf.append(chunk)
            size += len(chunk)

        for tier in ("A", "B", "C"):
            if not by_tier[tier]:
                continue
            put(f"\n━━ {TIER_BADGE[tier]} {len(by_tier[tier])}건 ━━")
            for p in by_tier[tier]:
                put("\n" + format_posting(p))

        if aside:
            put("\n━━ 🔅 AI 보류 " + str(len(aside)) + "건 ━━\n"
                "<i>관련이 낮다고 판단했습니다. 지우지 않고 제목만 남깁니다.</i>")
            for p in aside:
                why = p.get("ai_why") or ""
                put(f'\n· <a href="{esc(p.get("link") or "")}">'
                    f'{esc((p.get("title") or "")[:60])}</a>'
                    + (f" <i>— {esc(why)}</i>" if why else ""))
        flush()


    # ── 헬스 리포트 ────────────────────────────────────────────────
    #  예외를 삼켜서 조용히 0건이 되는 것이 이 시스템 최대의 실패 모드다.
    #  세 종류를 구분해 알린다.
    #    실패      : 예외 발생 (연속 횟수에 따라 심각도 상승)
    #    0건       : 정상 응답인데 한 건도 못 뽑음 → 구조 변경 유력
    #    수집 급감 : 정상이고 0건도 아니지만 과거 대비 급감 → 부분 파싱 실패
    srcs = health["sources"]
    # 'skipped' 는 아직 설정하지 않은 소스(예: API 키 미발급)로 고장이 아니다.
    # 경고에 섞으면 경고 피로가 생겨 진짜 고장을 흘려보게 된다.
    skipped = [h for h in srcs if h["status"] == "skipped"]
    broken = [h for h in srcs if h["status"] not in ("ok", "skipped")]

    # 언제 알릴지는 state.record_health 가 diag["alert"] 로 정한다.
    #   1회      : 알리지 않는다(일시 장애, 다음 실행이 메운다)
    #   2~4회    : 알린다(새로 생긴 고장)
    #   5회 이상 : 하루 1회만 알린다(만성 고장)
    # 실측: 중랑구가 83회 연속 실패하는 동안 경고를 83번 보냈고, 그 사이 다른
    # 소스의 고장이 그 안에 묻혔다. 만성 고장은 이미 아는 사실이니 반복하지
    # 않되 하루 1회는 남겨 잊히지 않게 한다.
    failed = [h for h in broken if h.get("diag", {}).get("alert")]
    muted = [h for h in broken if h not in failed]
    recovered = [h for h in srcs if h.get("diag", {}).get("recovered")]
    empty = [h for h in srcs if h["status"] == "ok" and h["rows"] == 0]
    # 0건은 이미 위에서 잡히므로 급감 목록에서는 뺀다
    dropped = [h for h in srcs
               if h.get("diag", {}).get("volume_drop") and h["rows"] > 0]

    if failed or empty or dropped or recovered:
        chronic = [h for h in failed
                   if h.get("diag", {}).get("consecutive_failures", 0) >= 3]
        icon = "🚨" if (chronic or empty) else "⚠️"
        if recovered and not (failed or empty or dropped):
            icon = "✅"
        parts = [f"{icon} <b>수집 상태 경고</b>",
                 f"정상 {health['ok']} / 전체 {health['total']} 소스"]
        if failed:
            parts.append("\n<b>수집 실패</b>")
            for h in failed[:20]:
                d = h.get("diag", {})
                n = d.get("consecutive_failures", 0)
                tag = f" · <b>{n}회 연속</b>" if n >= 3 else ""
                if d.get("chronic"):
                    tag += " · 만성(하루 1회만 알림)"
                if d.get("days_since_ok") is not None:
                    tag += f" · 마지막 정상 {d['days_since_ok']}일 전"
                parts.append(f"· {esc(h['name'])}{tag}\n  ↳ "
                             f"{esc((h.get('error') or h['status'])[:120])}")
        if empty:
            parts.append("\n<b>0건 반환 — 구조 변경 유력</b>")
            for h in empty[:20]:
                parts.append(f"· {esc(h['name'])}")
        if dropped:
            parts.append("\n<b>수집량 급감 — 부분 파싱 실패 의심</b>")
            for h in dropped[:20]:
                d = h["diag"]
                parts.append(f"· {esc(h['name'])}: {h['rows']}건 "
                             f"(평소 {d.get('baseline')}건)")
        if recovered:
            parts.append("\n<b>복구됨</b>")
            for h in recovered[:10]:
                k = h.get("diag", {}).get("consecutive_failures", 0)
                parts.append(f"· {esc(h['name'])} ({k}회 실패 후 정상)")
        if chronic or empty:
            parts.append("\n<i>sources.yml 의 해당 URL을 직접 열어 확인하세요. "
                         "403/429 가 반복되면 IP 차단일 수 있습니다.</i>")
        msgs.append("\n".join(parts))
    elif not postings:
        tail = f" · 미설정 {len(skipped)}개" if skipped else ""
        # 알림을 억제한 실패도 숨기지는 않는다 (건수만 표기)
        tail += (f" · 실패 {len(muted)}개(일시/만성 — 알림 억제)"
                 if muted else "")
        msgs.append(f"✅ <b>신규 공고 없음</b>  <i>{now_kst():%m/%d %H:%M}</i>\n"
                    f"{health['ok']}/{health['total']} 소스 정상 · "
                    f"{health['scanned']}건 확인{tail}")
    return msgs


def send(token: str, chat_id: str, messages: list[str],
         *, dry_run: bool = False) -> tuple[int, int]:
    """메시지를 순차 발송. (성공, 실패) 반환. 예외를 올리지 않는다."""
    sent = failed = 0
    for i, text in enumerate(messages):
        if dry_run:
            print(f"\n{'='*66}\n[DRY-RUN 메시지 {i+1}/{len(messages)}]\n{'='*66}\n{text}")
            sent += 1
            continue
        try:
            r = requests.post(
                API.format(token=token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=25,
            )
            if r.status_code == 200:
                sent += 1
            elif r.status_code == 400:
                # HTML 파싱 오류일 수 있다. 태그 하나 때문에 공고를 통째로
                # 잃는 것이 이 시스템에서 가장 비싼 실패다 — 태그를 벗겨
                # 한 번 더 보낸다. 모양은 나빠도 내용은 도착한다.
                log.warning("텔레그램 400 — 태그를 벗겨 재시도: %s", r.text[:200])
                plain = to_plain(text)
                r2 = requests.post(
                    API.format(token=token),
                    json={"chat_id": chat_id, "text": plain,
                          "disable_web_page_preview": True},
                    timeout=25,
                )
                if r2.status_code == 200:
                    sent += 1
                    log.warning("평문으로 발송했습니다 (서식 없음)")
                else:
                    failed += 1
                    log.error("평문 재시도도 실패 %s: %s",
                              r2.status_code, r2.text[:300])
            else:
                failed += 1
                log.error("텔레그램 발송 실패 %s: %s", r.status_code, r.text[:300])
        except requests.RequestException as e:
            failed += 1
            log.error("텔레그램 발송 예외: %s", e)
        time.sleep(1.1)          # 봇 rate limit 회피
    return sent, failed
