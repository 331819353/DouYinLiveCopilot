"""把服务端事件广播给所有已连接的浏览器页面。"""
import asyncio
import json
from collections import deque

from fastapi import WebSocket


class Bus:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.send_lock = asyncio.Lock()  # 握手快照和增量广播按顺序发，避免重连时漏掉插入事件
        self.recent_comments: deque = deque(maxlen=200)  # 新打开的页面补发最近评论
        self.recent_cards: deque = deque(maxlen=10)
        self.status: dict = {}
        self.taps: list = []  # 旁路监听，例如会话录制

    async def publish(self, msg: dict, *, clear_comments=False, clear_cards=False):
        async with self.send_lock:
            # 清理握手快照与发出切换事件使用同一把锁，重连不会取到旧房间的缓存。
            if clear_comments:
                self.recent_comments.clear()
                self.status.pop("stats", None)
                self.status.pop("source", None)
            if clear_cards:
                self.recent_cards.clear()
                self.status.pop("reply_status", None)
                self.status.pop("insert_status", None)
            for tap in self.taps:
                tap(msg)
            t = msg.get("type")
            if t == "comment":
                self.recent_comments.append(msg)
            elif t == "card":
                self.recent_cards.append(msg)
            elif t in ("source", "reply_status", "stats", "insert_status"):
                self.status[t] = msg
            data = json.dumps(msg, ensure_ascii=False)
            for ws in list(self.clients):
                try:
                    await ws.send_text(data)
                except Exception:
                    self.clients.discard(ws)

    def snapshot(self) -> list[dict]:
        return [*self.status.values(), *self.recent_comments, *self.recent_cards]
