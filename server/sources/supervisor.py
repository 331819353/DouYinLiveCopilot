"""管理评论采集子进程：启动、读取事件、断线退避重连、健康判断。"""
import asyncio
import json
import sys
import time
from pathlib import Path

from ..config import LOGS

WORKER = Path(__file__).resolve().parent / "webfetcher_worker.py"
SILENCE_SEC = 180   # 有数据帧但这么久没有评论：可能静默失效
DEAD_SEC = 90       # 这么久没有任何数据帧：连接可能假死


class SourceSupervisor:
    def __init__(self, on_event, live_id: str | None = None, mock_file: str | None = None):
        self.on_event = on_event
        self.live_id = live_id
        self.mock_file = mock_file
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "starting"
        self.restarts = 0
        self.open_since = None
        self.last_frame_t = None
        self.last_frames = 0
        self.last_chat_t = None
        self.error = None
        self.ended = False
        self._stopping = False

    @property
    def kind(self):
        return "mock" if self.mock_file is not None else "live"

    def _cmd(self):
        cmd = [sys.executable, str(WORKER)]
        if self.kind == "mock":
            cmd += ["--mock-seconds", "86400"]
            if self.mock_file:
                cmd += ["--mock-file", self.mock_file]
        else:
            cmd += ["--live-id", self.live_id]
        return cmd

    async def run(self):
        LOGS.mkdir(parents=True, exist_ok=True)
        backoff = 2
        with open(LOGS / "worker.log", "a", encoding="utf-8") as log:
            while not self._stopping:
                launched = time.time()
                self.proc = await asyncio.create_subprocess_exec(
                    *self._cmd(), stdout=asyncio.subprocess.PIPE, stderr=log)
                async for raw in self.proc.stdout:
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._track(rec)
                    await self.on_event(rec)
                code = await self.proc.wait()
                self.open_since = None
                if self.ended or self._stopping:
                    self.state = "ended" if self.ended else "stopped"
                    break
                if time.time() - launched > 60:
                    backoff = 2
                self.restarts += 1
                self.state = "reconnecting"
                self.error = f"采集进程退出（代码 {code}），{backoff} 秒后重连"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _track(self, rec):
        t, typ = rec.get("t", time.time()), rec.get("type")
        if typ == "open":
            self.open_since, self.last_frames, self.error = t, 0, None
            self.last_frame_t = t
        elif typ == "alive":
            if (rec.get("frames") or 0) > self.last_frames:
                self.last_frame_t = t
            self.last_frames = rec.get("frames") or 0
        elif typ == "chat":
            self.last_chat_t = self.last_frame_t = t
        elif typ == "stats":
            self.last_frame_t = t
        elif typ == "ended":
            self.ended = True
        elif typ == "fatal":
            self.error = "取不到直播间信息：可能未开播、房间号不对，或页面结构已变"

    def health(self) -> dict:
        now = time.time()
        if self.ended:
            state, note = "ended", "直播已结束"
        elif self.open_since is None:
            state, note = ("reconnecting", self.error or "正在连接") if self.restarts or self.error \
                else ("starting", "正在连接")
        elif self.last_frame_t and now - self.last_frame_t > DEAD_SEC:
            state, note = "dead", f"{DEAD_SEC} 秒没有收到任何数据，连接可能已假死"
        elif (self.last_chat_t or self.open_since) and now - (self.last_chat_t or self.open_since) > SILENCE_SEC:
            state, note = "quiet", f"超过 {SILENCE_SEC} 秒没有评论，请对照直播伴侣确认是否漏采"
        else:
            state, note = "ok", "采集正常"
        return {"type": "source", "kind": self.kind, "state": state, "note": note,
                "restarts": self.restarts}

    async def stop(self):
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
