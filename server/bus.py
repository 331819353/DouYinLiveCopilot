"""把服务端事件广播给所有已连接的浏览器页面。"""
import json
from collections import deque

from fastapi import WebSocket


class Bus:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.recent_comments: deque = deque(maxlen=200)  # 新打开的页面补发最近评论
        self.recent_cards: deque = deque(maxlen=10)
        self.status: dict = {}

    async def publish(self, msg: dict):
        t = msg.get("type")
        if t == "comment":
            self.recent_comments.append(msg)
        elif t == "card":
            self.recent_cards.append(msg)
        elif t in ("source", "reply_status", "stats"):
            self.status[t] = msg
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)

    def snapshot(self) -> list[dict]:
        return [*self.status.values(), *self.recent_comments, *self.recent_cards]
