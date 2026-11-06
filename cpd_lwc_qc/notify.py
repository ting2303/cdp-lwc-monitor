"""把問題傳到 Discord。用 Discord 頻道的 Webhook,不需要架機器人主機。

Webhook 網址等同密碼:只放在電腦上的 panel_config.json,不要放進會上傳的資料夾,也不會印出來。
沒有網路時訊息會留著,恢復後再送。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import List, Optional
from urllib.parse import urlparse

MAX_LENGTH = 1900  # Discord 單則上限 2000 字
MAX_QUEUE = 200
LEVEL_MARK = {"error": "🔴", "warn": "🟡", "info": "🟢"}


def valid_webhook(url: str) -> bool:
    parts = urlparse(url or "")
    if parts.scheme == "https" and parts.hostname in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"):
        return parts.path.startswith("/api/webhooks/")
    return False


def format_alert(alert: dict, site: str = "") -> str:
    lines = [f"{LEVEL_MARK.get(alert['level'], '🟡')} **{alert['title']}**" + (f"({site})" if site else ""), alert["detail"]]
    steps = alert.get("steps") or []
    if steps:
        lines.append("**建議:**")
        lines += [f"{i}. {step}" for i, step in enumerate(steps, 1)]
    return "\n".join(lines)[:MAX_LENGTH]


def format_resolved(title: str, since: str, site: str = "") -> str:
    return f"🟢 **已恢復:{title}**" + (f"({site})" if site else "") + f"\n從 {since} 開始的問題已解除。"


class Notifier:
    """保留待送訊息,送失敗不會弄丟。queue 是外部保存的 list。"""

    def __init__(self, url: Optional[str], queue: List[str], allow_local: bool = False, sleep=time.sleep):
        self.url = url if url and (valid_webhook(url) or allow_local) else None
        self.configured = bool(url)
        self.invalid = bool(url) and self.url is None
        self.queue = queue
        self.sleep = sleep
        self.last_error: Optional[str] = None

    def add(self, text: str) -> None:
        if self.url is None:
            return
        self.queue.append(text)
        del self.queue[:-MAX_QUEUE]

    def _send(self, text: str) -> bool:
        body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json", "User-Agent": "CDP-LWC-Monitor/1.0"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15):
                self.last_error = None
                return True
        except urllib.error.HTTPError as error:
            if error.code == 429:
                try:
                    wait = float(json.loads(error.read().decode("utf-8")).get("retry_after", 2))
                except (ValueError, AttributeError):
                    wait = 2.0
                self.sleep(min(wait, 30))
                self.last_error = "Discord 限制傳送速度"
            else:
                self.last_error = f"Discord 回應 {error.code}"
                if error.code in (400, 401, 403, 404):  # 網址錯了,重送也沒用,但保留訊息等設定修好
                    self.last_error += ",請檢查 Webhook 網址"
            return False
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            self.last_error = f"無法連到 Discord:{type(error).__name__}"
            return False

    def flush(self) -> int:
        sent = 0
        while self.url and self.queue:
            if not self._send(self.queue[0]):
                break
            self.queue.pop(0)
            sent += 1
        return sent
