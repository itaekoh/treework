#!/usr/bin/env python3
"""서울 공공기관 수목·나무의사 관련 공고 수집 → 텔레그램 발송.

설계 원칙
  1. 실패를 삼키지 않는다. 소스별 예외/0건을 집계해 텔레그램으로 경고한다.
     사이트 구조가 바뀌어 조용히 0건이 되는 것이 이 시스템 최대의 실패 모드다.
  2. 제목은 넓게 걷고 본문·첨부에서 등급을 올린다. '나무의사'는 직무명이
     아니라 자격 요건이고 대체로 HWP 첨부 안에만 있다(filters.py 참고).
  3. 과잘림보다 과보고를 택한다. 노이즈 키워드는 등급만 낮추고 버리지 않는다.

사용법
  python main.py --dry-run              # 발송 없이 콘솔 출력
  python main.py --dry-run --source seoul-job-public
  python main.py --dry-run --no-detail  # 상세/첨부 생략(빠른 점검)
  python main.py                        # 실제 발송

환경변수
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (--dry-run 이 아니면 필수)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import yaml

from treework import filters, notify
from treework.collectors import COLLECTORS, MissingKey, Posting, _within
from treework.fetcher import Fetcher
from treework.state import SeenStore, make_key

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("treework")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # pdfminer/pypdf 는 DEBUG 에서 PDF 내부 구조를 줄마다 찍어 로그를 뒤덮는다
    for noisy in ("urllib3", "pdfminer", "pypdf", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config(path: Path) -> tuple[dict, list[dict]]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {}) or {}
    sources = [s for s in (cfg.get("sources") or []) if s.get("enabled", True)]
    return defaults, sources


JOB_RE = re.compile(r"채용|모집|선발|구인|용역|위탁|공모")
TRUNCATED_RE = re.compile(r"(\.\.\.|…)\s*$")


def wants_deep_scan(src: dict) -> bool:
    """상세를 '전량' 받을지 여부.

    채용 전용 게시판은 애초에 채용 공고만 있으므로 제목에 키워드가 없어도
    상세를 받아 첨부까지 확인한다(자격 요건이 첨부에만 있는 케이스를 잡기 위함).
    고시공고 게시판은 공시송달·과태료 등 무관한 공고가 대량이라 표적 조회한다.
    """
    if "deep_scan" in src:
        return bool(src["deep_scan"])
    return "채용" in (src.get("name") or "")


def should_enrich(src: dict, p, deep: bool) -> bool:
    """상세를 받아올 공고를 고른다.

    전량 조회는 사이트 부하와 차단 위험이 크다. 대신 놓칠 위험이 실제로 있는
    경우만 표적 조회한다.
      1) 채용 전용 게시판 전량
      2) 제목에 키워드가 걸린 건
      3) 담당부서가 수목·녹지 소관인 채용/모집 공고 — 직무명에 수목 어휘가
         없어도 자격 요건에 나무의사가 있을 수 있다
      4) 제목이 잘린('...') 채용/모집 공고 — 잘린 뒷부분에 키워드가 있을 수 있다
    """
    if deep or filters.title_prefilter(p.title):
        return True
    if not JOB_RE.search(p.title or ""):
        return False
    return filters.dept_is_green(p.dept) or bool(TRUNCATED_RE.search(p.title or ""))


def collect_source(f: Fetcher, src: dict, defaults: dict, *,
                   with_detail: bool) -> tuple[list[Posting], dict]:
    """한 소스를 수집한다. (공고목록, 헬스) 반환."""
    health = {"id": src["id"], "name": src["name"], "status": "ok",
              "rows": 0, "detail": 0, "error": ""}
    handler = COLLECTORS.get(src["kind"])
    if not handler:
        health.update(status="error", error=f"알 수 없는 kind: {src['kind']}")
        return [], health

    collect, enrich = handler
    lookback = int(defaults.get("lookback_days", 21))
    try:
        postings = collect(f, src, lookback)
    except MissingKey as e:
        # 키를 아직 발급받지 않은 상태는 고장이 아니다. 매 실행 경고를 띄우면
        # 경고 피로가 생겨 진짜 고장을 흘려보게 된다.
        health.update(status="skipped", error=str(e)[:200])
        log.info("[%s] 건너뜀: %s", src["id"], e)
        return [], health
    except Exception as e:                            # noqa: BLE001
        health.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
        log.warning("[%s] 수집 실패: %s", src["id"], e)
        return [], health

    health["rows"] = len(postings)
    postings = [p for p in postings if _within(p.reg_date, lookback)]
    log.info("[%s] 목록 %d건 (lookback 내 %d건)",
             src["id"], health["rows"], len(postings))

    # 결과·경과 공고와 마감된 공고를 버린다. 상세 조회 이전에 걸러야
    # 실행 시간도 줄어든다(첨부 다운로드가 전체 시간의 대부분이다).
    # 상세 조회기가 없는 소스(나라장터)나 --no-detail 실행에서도 반드시 적용돼야
    # 하므로 아래 조기 반환보다 앞에 둔다.
    before = len(postings)
    postings = [p for p in postings
                if not filters.is_result_notice(p.title)
                and not filters.is_closed(p.due_date)]
    dropped = before - len(postings)
    if dropped:
        log.info("[%s] 결과공고·마감분 %d건 제외", src["id"], dropped)
    health["dropped"] = dropped

    if not with_detail or not enrich:
        return postings, health

    deep = wants_deep_scan(src)
    for p in postings:
        if not should_enrich(src, p, deep):
            continue
        try:
            enrich(f, p, src)
            health["detail"] += 1
        except Exception as e:                        # noqa: BLE001
            # 개별 상세 실패는 치명적이지 않다. 제목만으로 계속 판정한다.
            log.debug("[%s] 상세 실패 %r: %s", src["id"], p.title[:40], e)
    return postings, health


def main() -> int:
    ap = argparse.ArgumentParser(description="서울 수목·나무의사 공고 수집기")
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 출력만")
    ap.add_argument("--no-detail", action="store_true", help="상세/첨부 생략")
    ap.add_argument("--source", action="append", help="특정 소스 id만 실행")
    ap.add_argument("--config", default=str(ROOT / "sources.yml"))
    ap.add_argument("--state", default=str(ROOT / "seen.json"))
    ap.add_argument("--no-state-write", action="store_true",
                    help="상태 파일에 기록하지 않음(테스트용)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and not (token and chat_id):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다. "
                  "--dry-run 으로 먼저 점검하세요.")
        return 2

    defaults, sources = load_config(Path(args.config))
    if args.source:
        sources = [s for s in sources if s["id"] in set(args.source)]
        if not sources:
            log.error("--source 에 해당하는 소스가 없습니다.")
            return 2

    store = SeenStore(args.state)
    f = Fetcher(timeout=int(defaults.get("timeout", 25)),
                rate_limit_sec=float(defaults.get("rate_limit_sec", 1.2)),
                retries=int(defaults.get("retries", 2)))

    t0 = time.monotonic()
    health_rows, scanned, candidates = [], 0, []
    for src in sources:
        postings, health = collect_source(f, src, defaults,
                                         with_detail=not args.no_detail)
        # 이번 결과를 이력에 남기고 연속 실패·수집량 급감을 진단한다.
        # 'skipped'(키 미설정 등)는 고장이 아니므로 이력에 넣지 않는다.
        if health["status"] == "skipped":
            health["diag"] = {"consecutive_failures": 0, "days_since_ok": None,
                              "volume_drop": False, "baseline": None}
        else:
            health["diag"] = store.record_health(
                src["id"], ok=health["status"] == "ok", rows=health["rows"],
                error=health.get("error", ""))
        d = health["diag"]
        if d["consecutive_failures"] >= 3:
            log.error("[%s] %d회 연속 실패 — 구조 변경 가능성",
                      src["id"], d["consecutive_failures"])
        if d["volume_drop"]:
            log.warning("[%s] 수집량 급감: %d건 (평소 %s건)",
                        src["id"], health["rows"], d["baseline"])
        health_rows.append(health)
        scanned += len(postings)
        for p in postings:
            m = filters.classify(p.title, p.body, p.attach_text, p.dept)
            if not m.matched:
                continue
            p.tier, p.hits, p.demoted = m.tier, m.hits, m.demoted
            candidates.append(p)

    # ── 신규만 추린다 ────────────────────────────────────────────────
    #  '발송했다'고 기록하는 것은 실제 발송이 성공한 뒤로 미룬다. 먼저 기록하면
    #  발송이 실패했을 때 그 공고가 영구 누락된다.
    new: list[Posting] = []
    new_keys: list[tuple[str, Posting]] = []
    for p in candidates:
        key = make_key(p.source_id, p.title, p.reg_date)
        if store.is_new(key):
            new.append(p)
            new_keys.append((key, p))

    ok = sum(1 for h in health_rows if h["status"] == "ok")
    health = {"sources": health_rows, "ok": ok, "total": len(health_rows),
              "scanned": scanned}
    log.info("소스 %d/%d 정상 · 확인 %d건 · 키워드 매칭 %d건 · 신규 %d건 · %.1f초",
             ok, len(health_rows), scanned, len(candidates), len(new),
             time.monotonic() - t0)
    if f.insecure_hosts:
        log.warning("SSL 검증을 낮춘 호스트: %s", ", ".join(sorted(f.insecure_hosts)))

    # 신규는 오래된 것부터, 등급 높은 것 우선
    new.sort(key=lambda p: ({"A": 0, "B": 1, "C": 2}.get(p.tier or "C", 3),
                            p.reg_date or ""))
    payload = [{
        "title": p.title, "org": p.org, "dept": p.dept,
        "reg_date": p.reg_date, "due_date": p.due_date, "link": p.link,
        "tier": p.tier, "hits": p.hits, "demoted": p.demoted,
        "attach_names": p.attach_names, "attach_truncated": p.attach_truncated,
        "link_is_board": p.link_is_board, "amount": p.amount,
    } for p in new]

    messages = notify.build_messages(payload, health)
    sent, failed = notify.send(token, chat_id, messages, dry_run=args.dry_run)
    log.info("메시지 %d건 발송 (실패 %d건)", sent, failed)

    if args.dry_run or args.no_state_write:
        return 0

    if failed:
        # 발송에 실패한 공고를 '보낸 것'으로 기록하면 영구 누락이 된다.
        # 다만 소스 건강 이력은 발송 성공과 무관하게 반드시 남긴다 — 안 남기면
        # 텔레그램이 죽어 있는 동안 연속 실패 카운트가 쌓이지 않아 사이트 개편을
        # 놓친다. 그래서 seen 기록만 건너뛰고 저장은 한다.
        log.error("발송 실패 %d건 — 공고는 기록하지 않고 건강 이력만 저장합니다 "
                  "(다음 실행에서 재발송 시도).", failed)
        store.save()
        return 1

    for key, p in new_keys:
        store.mark(key, title=p.title, source_id=p.source_id, link=p.link)
    store.prune()
    store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
