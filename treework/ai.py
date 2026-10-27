"""AI 선별 계층 — 룰 필터를 통과한 소수 건만 다시 본다.

왜 1차 필터가 아니라 **최종 관문**인가
---------------------------------------
구독(Claude Code CLI) 경로는 호출마다 시스템 프롬프트·도구 정의를 통째로
싣는다. 실측: 공고 3건 평가에 cache_creation 63,689 토큰. 수집 원본 수천 건을
그대로 넘기면 구독 한도를 금방 쓴다. 그래서 룰이 먼저 수천 → 수십 건으로
줄이고, AI 는 그 소수를 **한 번에 묶어** 본다. 하루 호출 1~2회면 충분하다.

백엔드 선택 순서
----------------
  1. ANTHROPIC_API_KEY   → anthropic SDK (가장 싸다. Haiku 5.5 로 1회 약 0.6원)
  2. CLAUDE_CODE_OAUTH_TOKEN → `claude -p` CLI (구독 사용. 돈 대신 한도를 쓴다)
  3. 둘 다 없음          → 아무 일도 하지 않는다

**fail-open 원칙.** AI 가 없거나, 실패하거나, 응답이 깨졌거나, 일부 공고에만
답했으면 그 공고들은 **전부 그대로 통과**시킨다. 이 수집기의 목적은 누락을
없애는 것이고, AI 는 읽는 부담을 줄이는 보조 장치일 뿐이다. AI 고장이 공고
누락으로 이어지면 안 된다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("treework.ai")

MODEL = os.environ.get("TREEWORK_AI_MODEL", "claude-haiku-5-5")
# 이 점수 미만은 '참고'로 내린다. 버리지 않는다 — 제목은 그대로 보인다.
KEEP_SCORE = int(os.environ.get("TREEWORK_AI_KEEP", "3"))
MAX_ITEMS = 60          # 한 번에 평가할 최대 건수. 넘으면 나눠 호출한다.
# CLI(구독) 경로는 호출 1회가 $0.27 로 고정비가 크다. 나라장터 워크플로우가
# 평일 매시 돌기 때문에 1~2건 때문에 매번 호출하면 월 $70 까지 간다.
# 묶음이 이보다 작으면 CLI 는 건너뛴다 — 어차피 그 정도는 그냥 읽으면 된다.
# API 경로는 1회 0.6원이라 이 제한을 걸지 않는다.
MIN_BATCH_CLI = int(os.environ.get("TREEWORK_AI_MIN_BATCH", "3"))
CLI_TIMEOUT = 180

SYSTEM = (
    "너는 한국 공공기관 공고를 선별하는 보조자다. 사용자는 나무의사"
    "(수목 진단·치료 국가자격)이며 수목 관련 용역·채용과, 별도로 IT 용역"
    "입찰을 찾는다. 각 공고가 실제로 지원·수주할 가치가 있는지 0~5로 매겨라."
    "5=핵심 업무에 정확히 부합, 3=관련은 있으나 확인 필요, 0=완전 무관."
    "결과 공고·이미 마감된 건·지원 불가 업역은 낮게 매겨라."
)

INSTRUCT = (
    "아래 공고를 평가해라. 반드시 JSON 배열만 출력하고 다른 말은 하지 마라."
    ' 형식: [{"i": 번호, "fit": 0~5, "why": "15자 이내 한국어 근거"}]'
    " 모든 번호에 대해 빠짐없이 한 항목씩 출력해라."
)


def _prompt(items: list[dict]) -> str:
    lines = []
    for n, p in enumerate(items, 1):
        bits = [f'{n}. {p.get("title", "")}']
        if p.get("org"):
            bits.append(f'기관:{p["org"]}')
        if p.get("dept"):
            bits.append(f'부서:{p["dept"]}')
        if p.get("amount"):
            bits.append(f'금액:{p["amount"]}')
        lines.append(" | ".join(bits))
    return INSTRUCT + "\n\n" + "\n".join(lines)


def backend() -> str:
    """어떤 경로로 호출할지. 'api' / 'cli' / '' (없음)."""
    if os.environ.get("TREEWORK_AI", "").lower() in ("0", "off", "false"):
        return ""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "cli"
    return ""


def _parse(raw: str, n_items: int) -> dict[int, dict]:
    """응답에서 JSON 배열을 건져낸다. 모델이 설명을 덧붙여도 견딘다."""
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        raise ValueError(f"JSON 배열을 찾지 못함: {(raw or '')[:200]!r}")
    out: dict[int, dict] = {}
    for row in json.loads(m.group(0)):
        try:
            i = int(row["i"])
            if 1 <= i <= n_items:
                out[i] = {"fit": int(row.get("fit", 0)),
                          "why": str(row.get("why", ""))[:40]}
        except (KeyError, TypeError, ValueError):
            continue          # 한 행이 깨져도 나머지는 쓴다
    return out


def _call_api(items: list[dict]) -> str:
    import anthropic                       # 키가 있을 때만 필요하다
    client = anthropic.Anthropic()
    kw = dict(model=MODEL, max_tokens=4000, system=SYSTEM,
              messages=[{"role": "user", "content": _prompt(items)}])
    try:
        # 분류 작업이라 깊은 추론이 필요 없다. 구형 SDK 는 이 인자를 모른다.
        resp = client.messages.create(output_config={"effort": "low"}, **kw)
    except TypeError:
        log.debug("SDK 가 output_config 를 모릅니다 — 기본 설정으로 호출")
        resp = client.messages.create(**kw)
    u = resp.usage
    log.info("AI(api) 토큰 입력 %s 출력 %s", u.input_tokens, u.output_tokens)
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_cli(items: list[dict]) -> str:
    """구독 경로. `claude setup-token` 으로 만든 토큰을 쓴다."""
    # Windows 에서 `claude` 는 .cmd 래퍼라 PATH 이름만으로는 실행되지 않는다.
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI 를 찾을 수 없습니다")
    cmd = [exe, "-p", "--output-format", "json",
           "--model", MODEL, "--append-system-prompt", SYSTEM]
    r = subprocess.run(cmd, input=_prompt(items), capture_output=True,
                       text=True, encoding="utf-8", timeout=CLI_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI 종료코드 {r.returncode}: "
                           f"{(r.stderr or '')[:300]}")
    payload = json.loads(r.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI 오류: {str(payload)[:300]}")
    log.info("AI(cli) 비용 $%s", payload.get("total_cost_usd"))
    return payload.get("result", "")


def review(postings: list[dict]) -> str:
    """공고에 ai_fit / ai_why 를 채운다. 사용한 백엔드 이름을 돌려준다.

    실패하거나 백엔드가 없으면 아무것도 건드리지 않고 "" 를 돌려준다.
    그 경우 호출자는 모든 공고를 평소대로 내보내야 한다(fail-open).
    """
    which = backend()
    if not which or not postings:
        return ""
    if which == "cli" and len(postings) < MIN_BATCH_CLI:
        log.info("신규 %d건 — 구독 경로 최소 묶음(%d) 미만이라 AI 를 건너뜁니다",
                 len(postings), MIN_BATCH_CLI)
        return ""
    caller = _call_api if which == "api" else _call_cli
    done = 0
    for off in range(0, len(postings), MAX_ITEMS):
        chunk = postings[off:off + MAX_ITEMS]
        try:
            scored = _parse(caller(chunk), len(chunk))
        except Exception as e:                            # noqa: BLE001
            # 여기서 예외를 올리면 발송 전체가 죽는다. AI 는 보조 장치다.
            log.warning("AI 평가 실패(%s) — 이 묶음은 전부 통과시킵니다: %s",
                        which, e)
            continue
        for n, p in enumerate(chunk, 1):
            if n in scored:
                p["ai_fit"] = scored[n]["fit"]
                p["ai_why"] = scored[n]["why"]
                done += 1
    log.info("AI(%s) 평가 %d/%d건", which, done, len(postings))
    return which if done else ""


def is_demoted(p: dict) -> bool:
    """AI 가 낮게 본 건인가. 점수가 없으면(평가 실패) 항상 False."""
    fit = p.get("ai_fit")
    return fit is not None and fit < KEEP_SCORE
