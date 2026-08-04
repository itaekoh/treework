"""텔레그램 발송.

parse_mode 는 HTML을 쓴다. 공고 제목에 '[', '(', '-', '_' 가 대량으로 들어
있어서 Markdown/MarkdownV2 는 이스케이프 실수 한 번에 400 Bad Request 로
메시지가 통째로 유실된다. HTML은 &, <, > 세 글자만 막으면 안전하다.

메시지는 4096자 제한에 맞춰 공고 단위로 쪼갠다(공고가 잘리지 않게).
"""

from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 3800          # 4096에서 여유를 둔다
TIER_BADGE = {"A": "🔴 확정", "B": "🟡 유력", "C": "⚪ 확인필요"}


def esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def format_posting(p: dict) -> str:
    """공고 1건을 블록으로 만든다."""
    tier = p.get("tier") or "C"
    org, dept = (p.get("org") or "").strip(), (p.get("dept") or "").strip()
    # 나라장터는 공고기관과 수요기관이 같은 경우가 많아 그대로 두면 같은 이름이
    # 두 번 찍힌다. 부서가 기관명에 이미 포함돼 있으면 생략한다.
    if dept and (dept == org or dept in org or org in dept):
        dept = ""
    lines = [
        f"{TIER_BADGE.get(tier, tier)}  <b>{esc(p['title'])}</b>",
        f"🏢 {esc(org or '-')}" + (f" · {esc(dept)}" if dept else ""),
    ]
    reg, due = p.get("reg_date") or "", p.get("due_date") or ""
    if reg or due:
        lines.append(f"📅 등록 {esc(reg or '-')} · 마감 {esc(due or '확인불가')}")
    else:
        lines.append("📅 마감 확인불가")
    if p.get("amount"):
        lines.append(f"💰 {esc(p['amount'])}")

    hits = p.get("hits") or []
    if hits:
        lines.append(f"🔎 {esc(', '.join(hits[:6]))}")

    notes = []
    if p.get("attach_names"):
        notes.append(f"첨부 {len(p['attach_names'])}건")
    if p.get("attach_truncated"):
        notes.append("첨부 일부만 판독 — 원문 확인 권장")
    if p.get("demoted"):
        notes.append("노이즈 키워드로 등급 하향")
    if p.get("link_is_board"):
        notes.append("직접 링크 없음 · 게시판에서 검색")
    if notes:
        lines.append(f"ℹ️ {esc(' / '.join(notes))}")

    link = p.get("link") or ""
    if link:
        lines.append(f'🔗 <a href="{esc(link)}">공고 보기</a>')
    return "\n".join(lines)


def build_messages(postings: list[dict], health: dict) -> list[str]:
    """발송할 메시지 목록. 신규가 없어도 문제가 있으면 헬스 메시지를 만든다."""
    msgs: list[str] = []
    by_tier: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    for p in postings:
        by_tier.setdefault(p.get("tier") or "C", []).append(p)

    if postings:
        head = (f"🌳 <b>서울 공공기관 수목·나무의사 공고</b>\n"
                f"신규 {len(postings)}건 "
                f"(확정 {len(by_tier['A'])} · 유력 {len(by_tier['B'])} · "
                f"확인필요 {len(by_tier['C'])})")
        buf = [head]
        size = len(head)
        for tier in ("A", "B", "C"):
            if not by_tier[tier]:
                continue
            sec = f"\n━━━ {TIER_BADGE[tier]} ━━━"
            if size + len(sec) > LIMIT:
                msgs.append("\n".join(buf)); buf, size = [], 0
            buf.append(sec); size += len(sec)
            for p in by_tier[tier]:
                block = "\n" + format_posting(p)
                if size + len(block) > LIMIT:
                    msgs.append("\n".join(buf)); buf, size = [], 0
                buf.append(block); size += len(block)
        if buf:
            msgs.append("\n".join(buf))

    # ── 헬스 리포트 ────────────────────────────────────────────────
    #  예외를 삼켜서 조용히 0건이 되는 것이 이 시스템 최대의 실패 모드다.
    #  세 종류를 구분해 알린다.
    #    실패      : 예외 발생 (연속 횟수에 따라 심각도 상승)
    #    0건       : 정상 응답인데 한 건도 못 뽑음 → 구조 변경 유력
    #    수집 급감 : 정상이고 0건도 아니지만 과거 대비 급감 → 부분 파싱 실패
    srcs = health["sources"]
    # 'skipped' 는 아직 설정하지 않은 소스(예: API 키 미발급)로 고장이 아니다.
    # 경고에 섞으면 경고 피로가 생겨 진짜 고장을 흘려보게 된다.
    failed = [h for h in srcs if h["status"] not in ("ok", "skipped")]
    skipped = [h for h in srcs if h["status"] == "skipped"]
    empty = [h for h in srcs if h["status"] == "ok" and h["rows"] == 0]
    # 0건은 이미 위에서 잡히므로 급감 목록에서는 뺀다
    dropped = [h for h in srcs
               if h.get("diag", {}).get("volume_drop") and h["rows"] > 0]

    if failed or empty or dropped:
        chronic = [h for h in failed
                   if h.get("diag", {}).get("consecutive_failures", 0) >= 3]
        icon = "🚨" if (chronic or empty) else "⚠️"
        parts = [f"{icon} <b>수집 상태 경고</b>",
                 f"정상 {health['ok']} / 전체 {health['total']} 소스"]
        if failed:
            parts.append("\n<b>수집 실패</b>")
            for h in failed[:20]:
                d = h.get("diag", {})
                n = d.get("consecutive_failures", 0)
                tag = f" · <b>{n}회 연속</b>" if n >= 3 else ""
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
        if chronic or empty:
            parts.append("\n<i>sources.yml 의 해당 URL을 직접 열어 확인하세요. "
                         "403/429 가 반복되면 IP 차단일 수 있습니다.</i>")
        msgs.append("\n".join(parts))
    elif not postings:
        tail = f" · 미설정 {len(skipped)}개" if skipped else ""
        msgs.append(f"✅ 정상 동작 · 신규 공고 없음 "
                    f"({health['ok']}/{health['total']} 소스, "
                    f"{health['scanned']}건 확인){tail}")
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
            else:
                failed += 1
                log.error("텔레그램 발송 실패 %s: %s", r.status_code, r.text[:300])
        except requests.RequestException as e:
            failed += 1
            log.error("텔레그램 발송 예외: %s", e)
        time.sleep(1.1)          # 봇 rate limit 회피
    return sent, failed
