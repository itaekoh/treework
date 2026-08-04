#!/usr/bin/env python3
"""소스 자기 점검 — 파서가 아직 살아 있는지 확인한다.

공고 알림과 별개로 필요한 이유: 수목 관련 공고는 몇 주씩 안 나올 수 있다.
그 기간에 파서가 조용히 깨져도 '신규 없음' 메시지만 계속 오면 정상으로
착각한다. 이 점검은 '공고가 잡히는가'가 아니라 '목록이 파싱되는가'를 본다.

  python selftest.py            # 점검 후 문제 있으면 텔레그램 발송
  python selftest.py --dry-run  # 콘솔 출력만
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import yaml

from treework import notify
from treework.collectors import COLLECTORS, MissingKey
from treework.fetcher import Fetcher

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("selftest")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "sources.yml"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)-7s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {}) or {}
    sources = [s for s in (cfg.get("sources") or []) if s.get("enabled", True)]
    f = Fetcher(timeout=int(defaults.get("timeout", 25)),
                rate_limit_sec=float(defaults.get("rate_limit_sec", 1.2)))

    rows, t0 = [], time.monotonic()
    for src in sources:
        collect, _ = COLLECTORS.get(src["kind"], (None, None))
        entry = {"id": src["id"], "name": src["name"], "status": "ok",
                 "rows": 0, "error": "", "diag": {}}
        if collect is None:
            entry.update(status="error", error=f"알 수 없는 kind: {src['kind']}")
        else:
            try:
                got = collect(f, src, 3650)      # 날짜 제한 없이 목록만 확인
                entry["rows"] = len(got)
                if not got:
                    entry["error"] = "0건"
            except MissingKey as e:
                # 아직 설정하지 않은 소스는 고장이 아니다
                entry.update(status="skipped", error=str(e)[:160])
            except Exception as e:               # noqa: BLE001
                entry.update(status="error", error=f"{type(e).__name__}: {e}"[:160])
        rows.append(entry)
        mark = {"skipped": "SKIP"}.get(
            entry["status"], "OK  " if entry["rows"] else "FAIL")
        log.info("[%s] %-22s rows=%-4d %s", mark, src["id"], entry["rows"],
                 entry["error"])

    ok = sum(1 for r in rows if r["status"] == "ok" and r["rows"] > 0)
    skipped = [r for r in rows if r["status"] == "skipped"]
    total = len(rows)
    log.info("점검 완료: %d/%d 정상 (미설정 %d) · %.1f초",
             ok, total, len(skipped), time.monotonic() - t0)

    broken = [r for r in rows
              if r["status"] == "error" or (r["status"] == "ok" and r["rows"] == 0)]
    if broken:
        parts = [f"🔧 <b>주간 소스 점검</b>", f"정상 {ok} / 전체 {total}",
                 "\n<b>점검 실패</b>"]
        parts += [f"· {notify.esc(r['name'])} — {notify.esc(r['error'] or '0건')}"
                  for r in broken[:30]]
        parts.append("\n<i>sources.yml 의 해당 URL을 직접 열어 확인하세요.</i>")
    else:
        tail = f" (미설정 {len(skipped)}개 제외)" if skipped else ""
        parts = [f"🔧 <b>주간 소스 점검</b>",
                 f"전체 {total - len(skipped)}개 소스 정상 동작{tail}"]

    notify.send(os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                os.environ.get("TELEGRAM_CHAT_ID", ""),
                ["\n".join(parts)], dry_run=args.dry_run)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
