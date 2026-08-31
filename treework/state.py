"""중복 발송 방지 상태 파일.

링크만으로 키를 잡으면 세션ID나 파라미터 순서가 바뀔 때 같은 공고를 다시
보내게 된다. 그래서 (소스ID, 정규화 제목, 등록일) 해시를 1차 키로 쓰고
링크는 보조로만 본다.

TTL을 두어 파일이 무한히 커지지 않게 한다. GitHub 저장소에 커밋되는 파일이라
크기 관리가 필요하다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from pathlib import Path

from .timeutil import KST, today_kst

log = logging.getLogger(__name__)

TTL_DAYS = 180


def make_key(source_id: str, title: str, reg_date: str) -> str:
    norm = re.sub(r"[\s·ㆍ・/\-_()\[\]]+", "", title or "")
    basis = f"{source_id}|{norm}|{reg_date or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


class SeenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        # 소스별 이력: {source_id: {"ok": "YYYY-MM-DD", "fails": n, "rows": [..]}}
        self.health: dict[str, dict] = {}
        self._loaded_count = 0
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            log.info("상태 파일 없음 — 첫 실행으로 간주: %s", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = raw.get("seen", raw) if isinstance(raw, dict) else {}
            if isinstance(raw, dict):
                self.health = raw.get("health", {}) or {}
        except (json.JSONDecodeError, OSError) as e:
            # 손상된 상태 파일로 전체가 미발송 상태로 되돌아가면 대량 재발송이
            # 일어난다. 비우지 않고 그대로 실패시켜 원인을 드러낸다.
            log.error("상태 파일 읽기 실패 (%s) — 중복 발송 위험이 있어 중단", e)
            raise
        self._loaded_count = len(self.data)
        log.info("상태 파일 로드: %d건", self._loaded_count)

    def is_new(self, key: str) -> bool:
        return key not in self.data

    def mark(self, key: str, *, title: str, source_id: str, link: str = "") -> None:
        self.data[key] = {
            "t": (title or "")[:160],
            "s": source_id,
            "l": link[:400],
            "d": today_kst().isoformat(),
        }

    # ── 소스 건강 이력 ────────────────────────────────────────────────
    #  1회 실패는 일시 장애일 수 있지만 연속 실패는 구조 변경이다. 그리고
    #  status=ok 인데 수집량이 과거 대비 급감한 경우가 가장 위험하다 —
    #  '조용한 부분 실패'라서 예외도 0건 경고도 뜨지 않는다.
    ROWS_WINDOW = 10          # 최근 몇 회분 수집량을 기억할지
    DROP_RATIO = 0.4          # 과거 중간값의 이 비율 미만이면 급감으로 본다
    ALERT_AFTER = 2           # 연속 이 횟수부터 알린다 (1회는 일시 장애로 본다)
    CHRONIC_AFTER = 5         # 연속 이 횟수부터는 만성으로 보고 하루 1회만 알린다

    def record_health(self, source_id: str, *, ok: bool, rows: int,
                      error: str = "") -> dict:
        """이번 실행 결과를 이력에 반영하고 진단을 돌려준다.

        실패 사유를 함께 남긴다. 이게 없으면 로컬에서는 되는데 Actions 에서만
        실패하는 소스를 만났을 때 원인(차단/타임아웃/구조변경)을 구분할 수 없다.
        """
        h = self.health.setdefault(source_id, {"ok": "", "fails": 0, "rows": []})
        prev_rows = list(h.get("rows") or [])
        was_failing = int(h.get("fails", 0))
        diag = {"consecutive_failures": 0, "days_since_ok": None,
                "volume_drop": False, "baseline": None,
                "alert": False, "recovered": False, "chronic": False}

        if ok:
            h["fails"] = 0
            h["ok"] = today_kst().isoformat()
            h.pop("err", None)
            h.pop("err_at", None)
            h.pop("alerted", None)
            # 경고를 보냈던 소스가 살아나면 한 번 알린다. 그래야 사용자가
            # '아직 고장인가'를 매번 확인하지 않아도 된다.
            if was_failing >= self.ALERT_AFTER:
                diag["recovered"] = True
                diag["consecutive_failures"] = was_failing
            # 급감 판정은 과거 이력이 충분할 때만
            if len(prev_rows) >= 4:
                baseline = sorted(prev_rows)[len(prev_rows) // 2]   # 중간값
                diag["baseline"] = baseline
                if baseline >= 5 and rows < baseline * self.DROP_RATIO:
                    diag["volume_drop"] = True
            h["rows"] = (prev_rows + [rows])[-self.ROWS_WINDOW:]
        else:
            n = h["fails"] = int(h.get("fails", 0)) + 1
            h["err"] = (error or "")[:180]
            h["err_at"] = today_kst().isoformat()
            diag["consecutive_failures"] = n
            diag["chronic"] = n >= self.CHRONIC_AFTER
            if h.get("ok"):
                try:
                    last = datetime.strptime(h["ok"], "%Y-%m-%d").date()
                    diag["days_since_ok"] = (today_kst() - last).days
                except ValueError:
                    pass

            # 언제 알릴지.
            #   1회        : 알리지 않는다(일시 장애, 다음 실행이 메운다)
            #   2~4회      : 알린다(새로 생긴 고장)
            #   5회 이상   : **하루 1회만** 알린다
            # 만성 고장을 매 실행 알리면 경고가 일상이 되어 진짜 고장을 놓친다.
            # 실측: 중랑구가 83회 연속 실패하는 동안 경고를 83번 보냈고,
            # 그 사이 다른 소스의 고장이 그 안에 묻혔다.
            today = today_kst().isoformat()
            if n < self.ALERT_AFTER:
                diag["alert"] = False
            elif not diag["chronic"]:
                diag["alert"] = True
            else:
                diag["alert"] = h.get("alerted") != today
            if diag["alert"]:
                h["alerted"] = today
        return diag

    def prune(self) -> int:
        cutoff = (today_kst() - timedelta(days=TTL_DAYS)).isoformat()
        stale = [k for k, v in self.data.items()
                 if isinstance(v, dict) and v.get("d", "9999") < cutoff]
        for k in stale:
            del self.data[k]
        if stale:
            log.info("TTL 경과 %d건 정리 (%d일)", len(stale), TTL_DAYS)
        return len(stale)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "count": len(self.data),
            "health": self.health,
            "seen": self.data,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        log.info("상태 파일 저장: %d건 (%s)", len(self.data), self.path)
