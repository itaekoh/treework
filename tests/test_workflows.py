"""워크플로우 정의 회귀 테스트.

**이 테스트가 존재하는 이유.** step 의 `if:` 에 `secrets.` 를 쓰면 GitHub 이
워크플로우를 통째로 거부한다("No jobs were run"). 문법 오류가 아니라 컨텍스트
허용 목록 문제라 YAML 파서로는 잡히지 않고, 커밋한 뒤 다음 스케줄이 올 때까지
드러나지 않는다. 실측(2026-10-27~28): 수집이 하루 넘게 전면 중단됐고, 작업이
아예 뜨지 않아 `if: failure()` 알림조차 오지 않았다.

secrets 를 쓰려면 `env:` 로 받아 셸 안에서 판단해야 한다.

    python tests/test_workflows.py
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import yaml     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
fails = 0


def fail(msg: str) -> None:
    global fails
    fails += 1
    print(f"FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"PASS  {msg}")


assert FLOWS, "워크플로우 파일을 찾지 못했다"

for f in FLOWS:
    raw = f.read_text(encoding="utf-8")
    name = f.name

    # 1. 파싱되는가
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:                                   # noqa: BLE001
        fail(f"{name}: YAML 파싱 실패 — {e}")
        continue

    # 2. step 의 if: 에 secrets 가 섞여 있지 않은가 (가장 비싼 실수)
    bad = [i + 1 for i, line in enumerate(raw.split("\n"))
           if re.match(r"\s*if:", line) and "secrets." in line]
    if bad:
        fail(f"{name}: step if: 에 secrets 사용 ({bad}행) — env: 로 받아 셸에서 판단하라")
    else:
        ok(f"{name}: if: 에 secrets 없음")

    # 3. 잡이 하나라도 실제로 돌 수 있는가. 모든 잡이 걸러지면 'No jobs were run'.
    jobs = doc.get("jobs") or {}
    if not jobs:
        fail(f"{name}: 잡이 없다")
        continue
    always_on = [j for j, spec in jobs.items() if not (spec or {}).get("if")]
    if not always_on:
        fail(f"{name}: 모든 잡에 if: 가 걸려 있다 — 전부 건너뛰면 실행이 실패로 뜬다")
    else:
        ok(f"{name}: 조건 없는 잡 {len(always_on)}개")

    # 4. 잡 수준 if 가 참조하는 cron 이 실제 schedule 에 있는가.
    #    cron 시각을 옮기고 if 를 안 고치면 그 잡은 영영 돌지 않는다.
    crons = {c.get("cron") for c in
             ((doc.get(True) or doc.get("on") or {}).get("schedule") or [])}
    for jid, spec in jobs.items():
        cond = str((spec or {}).get("if") or "")
        for m in re.finditer(r"github\.event\.schedule\s*==\s*'([^']+)'", cond):
            if m.group(1) not in crons:
                fail(f"{name}: 잡 '{jid}' 이 없는 cron {m.group(1)!r} 을 기다린다 "
                     f"(실제: {sorted(crons)})")
            else:
                ok(f"{name}: 잡 '{jid}' 의 cron 참조가 schedule 과 일치")

# 5. 회귀 테스트가 수집 워크플로우 앞단에서 실제로 돌고 있는가.
#    테스트를 만들어 두고 등록을 잊으면 없는 것과 같다.
for name in ("cron.yml", "g2b.yml"):
    raw = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    missing = [t.name for t in sorted((ROOT / "tests").glob("test_*.py"))
               if f"tests/{t.name}" not in raw]
    if missing:
        fail(f"{name}: 등록되지 않은 테스트 {missing}")
    else:
        ok(f"{name}: 모든 테스트가 등록돼 있다")

# 6. 두 수집 워크플로우가 서로 다른 상태 파일을 쓰는가.
#    같은 파일을 쓰면 겹쳐 돌 때 rebase 가 충돌하고, 한 번 충돌하면 리베이스가
#    걸린 채 남아 재시도가 전부 즉시 실패한다 — 그 실행분 상태가 통째로 버려져
#    다음 실행에서 전부 재발송된다(실측: 2026-10-28, 수목 실행분 61건).
#    또 '쓰는 파일'과 '커밋하는 파일'이 어긋나면 상태가 영영 저장되지 않는다.
states: dict[str, str] = {}
for name in ("cron.yml", "g2b.yml"):
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / name)
                         .read_text(encoding="utf-8"))
    steps = ((doc.get("jobs") or {}).get("collect") or {}).get("steps") or []
    written = committed = None
    for s in steps:
        run = str((s or {}).get("run") or "")
        if "main.py" in run:
            m = re.search(r"--state\s+(\S+)", run)
            written = m.group(1) if m else "seen.json"     # main.py 의 기본값
        if "git push" in run:
            committed = ((s or {}).get("env") or {}).get("STATE")
    if not written:
        fail(f"{name}: main.py 를 실행하는 단계를 찾지 못했다")
    elif written != committed:
        fail(f"{name}: 쓰는 파일({written})과 커밋하는 파일({committed})이 다르다 "
             f"— 상태가 저장되지 않아 매 실행 재발송된다")
    else:
        ok(f"{name}: 상태 파일 {written} 을 쓰고 같은 파일을 커밋한다")
        states[name] = written

if len(states) == 2 and len(set(states.values())) == 1:
    fail(f"수집 워크플로우가 상태 파일을 공유한다 ({states}) — 동시 실행 시 "
         f"한쪽 실행분이 통째로 버려진다")
elif len(states) == 2:
    ok(f"수집 워크플로우의 상태 파일이 분리돼 있다 ({', '.join(states.values())})")

print(f"\n실패 {fails}건")
sys.exit(1 if fails else 0)
