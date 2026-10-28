"""수집이 **통째로 멈춘 것**을 알아차린다.

왜 따로 필요한가. 기존 `if: failure()` 텔레그램 알림은 **작업이 떠야** 울린다.
실측으로 그게 안 울리는 경우가 두 번 있었다.

  · 2026-10-27  워크플로우 YAML 이 GitHub 에 거부됨("No jobs were run").
                거부된 워크플로우는 **스케줄 자체가 등록되지 않아** 하루 넘게
                한 번도 돌지 않았고, 작업이 안 떴으니 실패 알림도 없었다.
  · 2026-10-28  수집·발송은 성공하고 뒷정리만 실패 → 실패 알림은 울렸지만
                '알림이 안 온다'와는 무관한 내용이었다.

두 번 다 사용자는 **GitHub 메일로** 알았다. 즉 '며칠째 조용하다'를 보는 눈이
없었다. 이 스크립트가 그 눈이다.

## 무엇을 보는가

`event=schedule` 로 **실제로 뜬 실행**만 센다. 성공·실패는 보지 않는다.

  · 떠서 깨진 경우   → 기존 `if: failure()` 알림이 담당한다
  · 아예 안 뜬 경우  → 여기가 담당한다

수동 실행(workflow_dispatch)은 세지 않는다. 손으로 돌려서 성공해도 **스케줄이
등록돼 있다는 증거가 되지 않기 때문이다** — 2026-10-27 이 정확히 그 상황이었다.

## 언제 알리는가

"마지막 실행 이후 **통째로 지나간 영업일**이 하나라도 있으면" 알린다.

벽시계 시간으로 자르면 안 된다. 금요일 마지막 실행과 월요일 첫 실행 사이는
정상이어도 70시간이 넘는다. 거꾸로 GitHub cron 은 4~7시간씩 밀리므로 '오늘
몇 시까지 안 오면' 류의 판정도 오경보를 낸다. 날짜 단위로 보는 것이 둘 다
피하는 유일한 방법이다. 오늘은 아직 안 끝났으므로 세지 않는다.

## 월요일 생존 신호

조용한 감시 장치는 '정상'과 '고장'을 구분할 수 없다. 그래서 월요일에는 아무
문제가 없어도 한 줄 보낸다. 그게 끊기면 감시 장치가 죽은 것이다.

    python watchdog.py              # 평소 (GitHub Actions)
    python watchdog.py --dry-run    # 보내지 않고 판정만 출력
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta

import requests

from treework.notify import send
from treework.timeutil import KST, is_business_day

log = logging.getLogger("watchdog")

API = "https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs"

# 감시 대상. 이름은 텔레그램에 그대로 나간다.
WATCHED = [("cron.yml", "수목 공고 수집"), ("g2b.yml", "나라장터 수집")]

# 조회 범위. 하루 13회(수목 4 + 나라장터 9) 도므로 50건이면 며칠치가 넉넉히
# 들어온다. 긴 연휴 뒤에도 마지막 실행을 놓치지 않을 만큼은 된다.
PER_PAGE = 50


def fetch_runs(repo: str, wf: str, token: str = "") -> list[dict]:
    """해당 워크플로우의 **스케줄 실행** 목록. 최신순."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(API.format(repo=repo, wf=wf), headers=headers,
                     params={"event": "schedule", "per_page": str(PER_PAGE)},
                     timeout=25)
    r.raise_for_status()
    return (r.json() or {}).get("workflow_runs") or []


def last_fired(runs: list[dict]) -> datetime | None:
    """마지막으로 **실제로 뜬** 스케줄 실행 시각 (KST).

    취소된 실행은 세지 않는다 — 떴지만 아무것도 수집하지 않았으므로
    '돌았다'고 볼 수 없다. 실패는 센다(뜨긴 떴고, 그건 실패 알림 담당이다).
    """
    best: datetime | None = None
    for run in runs:
        if (run.get("conclusion") or "") == "cancelled":
            continue
        raw = run.get("created_at") or ""
        try:
            t = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        t += timedelta(hours=9)                 # API 는 UTC, 우리는 KST(naive)
        if best is None or t > best:
            best = t
    return best


def missed_business_days(last: datetime | None, now: datetime) -> list[date]:
    """마지막 실행 이후 **통째로 지나간** 영업일.

    오늘은 아직 끝나지 않았으므로 제외한다. 마지막 실행 기록이 아예 없으면
    (조회 범위 밖이거나 한 번도 안 돈 것) 최근 영업일들을 돌려준다.
    """
    start = (last.date() if last else now.date() - timedelta(days=7))
    out: list[date] = []
    d = start + timedelta(days=1)
    while d < now.date():
        if is_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def silent_hours(repo: str = "", token: str = "", wf: str = "watchdog.yml",
                 now: datetime | None = None) -> float | None:
    """해당 워크플로우가 마지막으로 뜬 뒤 몇 시간 지났는가.

    **감시 장치 자신을 감시하기 위한 것이다.** 이 스크립트는 죽으면 아무 말도
    못 한다. 그래서 주간 자기점검(selftest.py)이 이 함수로 되묻는다 —
    수집이 멈추면 감시 장치가, 감시 장치가 멈추면 자기점검이 알린다.

    감시 장치는 주말 포함 매일 도므로 영업일 계산이 필요 없다.
    조회에 실패하면 None 을 준다. '모른다'를 '죽었다'로 보고하면 오경보가 된다.
    """
    now = now or datetime.now(KST).replace(tzinfo=None)
    try:
        last = last_fired(fetch_runs(repo or os.environ.get(
            "GITHUB_REPOSITORY", "") or "itaekoh/treework",
            wf, token or os.environ.get("GITHUB_TOKEN", "")))
    except Exception as e:                               # noqa: BLE001
        log.warning("[%s] 생존 확인 실패: %s", wf, e)
        return None
    if last is None:
        return None
    return (now - last).total_seconds() / 3600


def build_report(findings: list[tuple[str, list[date], datetime | None]],
                 now: datetime) -> str:
    """알림 본문. 무엇이 며칠 빠졌는지와, 바로 할 수 있는 일을 적는다."""
    lines = ["\U0001F6A8 <b>수집이 멈춰 있습니다</b>", ""]
    for label, missed, last in findings:
        days = " · ".join(f"{d:%m/%d}" for d in missed)
        when = f"{last:%m/%d %H:%M}" if last else "조회 범위 안에 없음"
        lines.append(f"<b>{label}</b>")
        lines.append(f"  빠진 영업일 {len(missed)}일 — {days}")
        lines.append(f"  마지막 실행 {when}")
        lines.append("")
    lines += [
        "스케줄이 아예 안 뜨고 있습니다. 워크플로우 YAML 이 거부되면 "
        "스케줄이 등록되지 않고, 작업이 뜨지 않으니 실패 알림도 오지 않습니다.",
        "",
        "<a href=\"https://github.com/itaekoh/treework/actions\">"
        "Actions 열기</a> → 해당 워크플로우 → <b>Run workflow</b> 로 "
        "수동 실행하면 원인이 바로 드러납니다.",
    ]
    return "\n".join(lines)


def build_heartbeat(states: list[tuple[str, datetime | None]],
                    now: datetime) -> str:
    """월요일 생존 신호. 이게 끊기면 감시 장치가 죽은 것이다."""
    parts = [f"✅ <b>감시 정상</b> ({now:%m/%d} 월)"]
    for label, last in states:
        when = f"{last:%m/%d %H:%M}" if last else "기록 없음"
        parts.append(f"  {label} — 마지막 실행 {when}")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="보내지 않고 판정만 출력")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    repo = args.repo or "itaekoh/treework"
    token = os.environ.get("GITHUB_TOKEN", "")
    now = datetime.now(KST).replace(tzinfo=None)

    findings: list[tuple[str, list[date], datetime | None]] = []
    states: list[tuple[str, datetime | None]] = []
    for wf, label in WATCHED:
        try:
            runs = fetch_runs(repo, wf, token)
        except Exception as e:                           # noqa: BLE001
            # 조회에 실패했다고 '멈췄다'고 알리면 안 된다. API 장애로 오경보를
            # 내면 다음부터 이 알림을 믿지 않게 된다 — 그게 더 위험하다.
            log.error("[%s] 실행 이력 조회 실패 — 판정을 건너뜁니다: %s", wf, e)
            continue
        last = last_fired(runs)
        missed = missed_business_days(last, now)
        states.append((label, last))
        log.info("[%s] 마지막 스케줄 실행 %s · 빠진 영업일 %d일",
                 wf, f"{last:%Y-%m-%d %H:%M}" if last else "없음", len(missed))
        if missed:
            findings.append((label, missed, last))

    if not states:
        log.error("모든 조회가 실패했습니다 — 알림을 보내지 않습니다")
        return 1

    messages: list[str] = []
    if findings:
        messages.append(build_report(findings, now))
    elif now.weekday() == 0:
        messages.append(build_heartbeat(states, now))

    if not messages:
        log.info("이상 없음 — 보낼 것이 없습니다")
        return 0

    token_tg = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and not (token_tg and chat):
        log.error("텔레그램 설정이 없습니다")
        return 1
    sent, failed = send(token_tg, chat, messages, dry_run=args.dry_run)
    log.info("메시지 %d건 발송 (실패 %d건)", sent, failed)
    # 수집이 멈춘 것은 비정상이므로 실행도 실패로 남긴다. Actions 목록에서
    # 빨간 X 로 보이는 편이 눈에 띈다.
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
