"""管理评论采集子进程：启动、读取事件、断线退避重连、健康判断。"""
import asyncio
import json
import sys
import time
from contextlib import suppress
from pathlib import Path

from ..config import LOGS

WORKER = Path(__file__).resolve().parent / "webfetcher_worker.py"
LIVE_FETCHER = WORKER.parents[2] / "vendor" / "DouyinLiveWebFetcher" / "liveMan.py"
SILENCE_SEC = 180   # 有数据帧但这么久没有评论：可能静默失效
DEAD_SEC = 90       # 这么久没有任何数据帧：连接可能假死


class SourceSupervisor:
    def __init__(self, on_event, live_id: str | None = None, mock_file: str | None = None):
        self.on_event = on_event
        self.live_id = live_id
        self.mock_file = mock_file
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.open_since = None
        self.last_frame_t = None
        self.last_frames = 0
        self.last_chat_t = None
        self.error = None
        self.ended = False
        self._stopping = asyncio.Event()

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
            while not self._stopping.is_set():
                # 上一条连接的心跳和评论时间不能影响新连接的健康判断。
                self.open_since = self.last_frame_t = self.last_chat_t = None
                self.last_frames = 0
                self.error = None
                try:
                    self.proc = await asyncio.create_subprocess_exec(
                        *self._cmd(), stdout=asyncio.subprocess.PIPE, stderr=log)
                    if self._stopping.is_set():
                        self._kill_worker()
                    reason = await self._read_worker()
                except OSError as e:
                    reason = f"采集进程启动或读取失败（{e}）"
                finally:
                    # 先结束并回收旧进程，再重连。读取中断时也要排空管道。
                    if self.proc:
                        self._kill_worker()
                        await self.proc.communicate()
                        self.proc = None
                if self.ended or self._stopping.is_set():
                    break
                if self.open_since is not None and self.last_frame_t - self.open_since > 60:
                    backoff = 2
                self.open_since = None
                self.restarts += 1
                self.error = f"{reason}，{backoff} 秒后重连"
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), backoff)
                backoff = min(backoff * 2, 60)

    async def _read_worker(self):
        loop = asyncio.get_running_loop()
        live = self.kind == "live"
        try:
            # 未连上时也有限时。只有实际收到新数据帧才续期，空心跳不能掩盖假死。
            async with asyncio.timeout(DEAD_SEC if live else None) as watchdog:
                async for raw in self.proc.stdout:
                    try:
                        rec = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    previous_frame = self.last_frame_t
                    self._track(rec)
                    if live and self.last_frame_t != previous_frame:
                        watchdog.reschedule(loop.time() + DEAD_SEC)
                    await self.on_event(rec)
                    if self.ended:
                        return "直播已结束"  # 收到明确的下播事件后直接收尾
                code = await self.proc.wait()
        except TimeoutError:
            if not watchdog.expired():
                raise
            stalled = "收到新数据帧" if self.open_since is not None else "连上直播间"
            return f"{DEAD_SEC} 秒内未{stalled}"
        return self.error or f"采集进程退出（代码 {code}）"

    def _track(self, rec):
        # 看门狗和状态灯使用本机收到事件的单调时钟，避免系统校时影响。
        t, typ = time.monotonic(), rec.get("type")
        if typ == "open":
            self.open_since, self.last_frames, self.last_chat_t, self.error = t, 0, None, None
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
        now = time.monotonic()
        if self.ended:
            state, note = "ended", "直播已结束"
        elif self._stopping.is_set():
            state, note = "stopped", "采集已停止"
        elif self.open_since is None:
            state, note = ("reconnecting", self.error or "正在连接") if self.restarts or self.error \
                else ("starting", "正在连接")
        elif self.last_frame_t is not None and now - self.last_frame_t > DEAD_SEC:
            state, note = "dead", f"{DEAD_SEC} 秒没有收到任何数据，连接可能已假死"
        elif (self.last_chat_t or self.open_since) and now - (self.last_chat_t or self.open_since) > SILENCE_SEC:
            state, note = "quiet", f"超过 {SILENCE_SEC} 秒没有评论，请对照直播伴侣确认是否漏采"
        else:
            state, note = "ok", "采集正常"
        return {"type": "source", "kind": self.kind, "state": state, "note": note, "room": self.live_id,
                "restarts": self.restarts}

    def _kill_worker(self):
        if self.proc and self.proc.returncode is None:
            with suppress(ProcessLookupError):  # 子进程可能刚好已自行退出
                self.proc.kill()

    async def stop(self):
        self._stopping.set()
        self._kill_worker()  # 采集进程可能卡在原库中，主进程负责落盘和收尾
