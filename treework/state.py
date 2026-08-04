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
from datetime import date, datetime, timedelta
from pathlib import Path

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
            "d": date.today().isoformat(),
        }

    # ── 소스 건강 이력 ────────────────────────────────────────────────
    #  1회 실패는 일시 장애일 수 있지만 연속 실패는 구조 변경이다. 그리고
    #  status=ok 인데 수집량이 과거 대비 급감한 경우가 가장 위험하다 —
    #  '조용한 부분 실패'라서 예외도 0건 경고도 뜨지 않는다.
    ROWS_WINDOW = 10          # 최근 몇 회분 수집량을 기억할지
    DROP_RATIO = 0.4          # 과거 중간값의 이 비율 미만이면 급감으로 본다

    def record_health(self, source_id: str, *, ok: bool, rows: int) -> dict:
        """이번 실행 결과를 이력에 반영하고 진단을 돌려준다."""
        h = self.health.setdefault(source_id, {"ok": "", "fails": 0, "rows": []})
        prev_rows = list(h.get("rows") or [])
        diag = {"consecutive_failures": 0, "days_since_ok": None,
                "volume_drop": False, "baseline": None}

        if ok:
            h["fails"] = 0
            h["ok"] = date.today().isoformat()
            # 급감 판정은 과거 이력이 충분할 때만
            if len(prev_rows) >= 4:
                baseline = sorted(prev_rows)[len(prev_rows) // 2]   # 중간값
                diag["baseline"] = baseline
                if baseline >= 5 and rows < baseline * self.DROP_RATIO:
                    diag["volume_drop"] = True
            h["rows"] = (prev_rows + [rows])[-self.ROWS_WINDOW:]
        else:
            h["fails"] = int(h.get("fails", 0)) + 1
            diag["consecutive_failures"] = h["fails"]
            if h.get("ok"):
                try:
                    last = datetime.strptime(h["ok"], "%Y-%m-%d").date()
                    diag["days_since_ok"] = (date.today() - last).days
                except ValueError:
                    pass
        return diag

    def prune(self) -> int:
        cutoff = (date.today() - timedelta(days=TTL_DAYS)).isoformat()
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
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "count": len(self.data),
            "health": self.health,
            "seen": self.data,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        log.info("상태 파일 저장: %d건 (%s)", len(self.data), self.path)
