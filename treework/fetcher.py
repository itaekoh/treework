"""HTTP 계층 — 재시도, SSL 폴백, 요청 간격.

공공 사이트를 대상으로 하므로 요청 간격을 반드시 둔다. 그리고 서울 자치구
사이트 중 일부는 인증서 체인이 낡아 표준 검증에 실패한다(실측: 노원구).
그 경우에만 검증을 낮춰 재시도하고, 낮췄다는 사실을 결과에 남긴다.
"""

from __future__ import annotations

import logging
import threading
import time

import requests
import urllib3

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class Fetcher:
    def __init__(self, *, timeout: int = 25, rate_limit_sec: float = 1.2,
                 retries: int = 2):
        self.timeout = timeout
        self.rate_limit_sec = rate_limit_sec
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self._lock = threading.Lock()
        self._last = 0.0
        self.insecure_hosts: set[str] = set()

    def _wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.rate_limit_sec:
                time.sleep(self.rate_limit_sec - gap)
            self._last = time.monotonic()

    def request(self, method: str, url: str, *, check_status: bool = True,
                **kw) -> requests.Response:
        """마지막 예외를 그대로 올린다. 호출측이 소스별로 기록한다.

        check_status=False 는 4xx/5xx 응답도 그대로 돌려준다. 공공데이터포털처럼
        오류를 403 + 본문 JSON으로 알려주는 API 는 본문을 읽어야 원인을 알 수 있다.
        """
        kw.setdefault("timeout", self.timeout)
        # 이 호스트가 이미 SSL 검증에 실패한 적 있으면 처음부터 낮춰서 부른다.
        # 그러지 않으면 요청마다 실패 1회를 반복해 요청 수가 두 배가 된다.
        if requests.utils.urlparse(url).netloc in self.insecure_hosts:
            kw.setdefault("verify", False)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                r = self.session.request(method, url, **kw)
                if check_status:
                    r.raise_for_status()
                return r
            except requests.exceptions.SSLError as e:
                last = e
                # 낡은 인증서 체인 — 이 호스트에 한해 검증을 낮춰 재시도
                if kw.get("verify", True):
                    host = requests.utils.urlparse(url).netloc
                    log.warning("SSL 검증 실패 → %s 는 verify=False 로 재시도", host)
                    self.insecure_hosts.add(host)
                    urllib3.disable_warnings(
                        urllib3.exceptions.InsecureRequestWarning)
                    kw["verify"] = False
                    continue
            except requests.RequestException as e:
                last = e
                status = getattr(e.response, "status_code", None)
                if status and 400 <= status < 500 and status != 429:
                    break          # 클라이언트 오류는 재시도 무의미
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise last                  # type: ignore[misc]

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    @staticmethod
    def text_of(r: requests.Response) -> str:
        """한글 인코딩 보정. 일부 구청은 charset 헤더가 없거나 틀리다."""
        if r.encoding and r.encoding.lower() in ("iso-8859-1", "ascii"):
            for enc in ("utf-8", "euc-kr", "cp949"):
                try:
                    return r.content.decode(enc)
                except UnicodeDecodeError:
                    continue
        return r.text
