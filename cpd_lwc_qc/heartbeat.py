"""外部心跳:定時向外部服務報平安。筆電整台掛掉時,由外部服務發現沒收到並通知你。

建議用 Healthchecks.io(開源,可用免費方案,也可以自己架),在它的網站把通知接到 Discord。
網址等同密碼:只放在 panel_config.json,不會回傳給網頁,也不會寫進診斷報告。
只送一次空的請求,不含任何觀測資料。
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

FAILS_TO_ALERT = 3


def valid_url(url: str) -> bool:
    parts = urlparse(url or "")
    return parts.scheme == "https" and bool(parts.hostname)


class Heartbeat:
    def __init__(self, url: Optional[str], interval: float = 300, allow_local: bool = False, clock=time.monotonic):
        self.allow_local = allow_local
        self.interval = float(interval)
        self.clock = clock
        self.url: Optional[str] = None
        self.configured = False
        self.fails = 0
        self.last_error: Optional[str] = None
        self.last_ok: Optional[float] = None
        self.next_at = 0.0
        self.set_url(url)

    def set_url(self, url: Optional[str]) -> None:
        self.configured = bool(url)
        self.url = url if url and (valid_url(url) or self.allow_local) else None
        self.fails = 0
        self.last_error = "網址格式不對" if self.configured and self.url is None else None
        self.next_at = 0.0

    def ping(self) -> bool:
        if self.url is None:
            return False
        request = urllib.request.Request(self.url, headers={"User-Agent": "CDP-LWC-Monitor/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            self.fails += 1
            # 錯誤訊息只留類型,避免把網址帶進紀錄
            self.last_error = type(error).__name__
            return False
        self.fails = 0
        self.last_error = None
        self.last_ok = time.time()
        return True

    def step(self) -> None:
        now = self.clock()
        if self.url is not None and now >= self.next_at:
            self.next_at = now + self.interval
            self.ping()

    @property
    def failing(self) -> bool:
        return self.url is not None and self.fails >= FAILS_TO_ALERT
