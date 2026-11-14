"""会话录制：把一场直播里的事件按时间写进 data/sessions/<开始时间>/，供下播复盘。

记录内容：评论（不含昵称，用户 ID 只有哈希）、在线人数、主播读到的段落、
AI 卡片和主播对卡片的操作、采集状态变化。只存本机，启动时删除 7 天前的会话。
"""
import json
import shutil
import time

from .config import ROOT

SESSIONS = ROOT / "data" / "sessions"
RETENTION_DAYS = 7


def cleanup_old():
    if not SESSIONS.exists():
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for d in SESSIONS.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d)
            print(f"已删除 {RETENTION_DAYS} 天前的会话记录：{d.name}")


class SessionRecorder:
    def __init__(self, script_data: dict, script_path, mode: str, room: str | None = None):
        cleanup_old()
        # 页面可在一秒内换稿或换房，仍要为每份文案与房间组合保留独立快照。
        self.dir = SESSIONS / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000000:09d}"
        self.dir.mkdir(parents=True, exist_ok=True)
        # 保存一份文案快照：之后文案被修改，复盘仍能对上当时的段落
        (self.dir / "script.json").write_text(json.dumps(script_data, ensure_ascii=False), "utf-8")
        (self.dir / "meta.json").write_text(json.dumps({
            "started": time.time(), "mode": mode, "room": room,
            "script_file": str(script_path.relative_to(ROOT)) if script_path.is_relative_to(ROOT) else str(script_path),
            "script_title": script_data.get("title", ""),
        }, ensure_ascii=False), "utf-8")
        self.f = open(self.dir / "events.jsonl", "a", encoding="utf-8", buffering=1)
        self.last_source_state = None
        self.last_reading = None

    def log(self, type_: str, **fields):
        self.f.write(json.dumps({"t": round(time.time(), 3), "type": type_, **fields},
                                ensure_ascii=False) + "\n")

    def comment(self, c: dict):
        # 评论由入口直接记录：这里有用户 ID 哈希（用于统计人数），推给页面的消息里没有
        self.log("comment", id=c["id"], uid=c.get("uid", ""), text=c["text"], q=c.get("q", False),
                 filtered=c.get("filtered"), source=c.get("source"))

    def tap(self, msg: dict):
        """挂在事件总线上，挑出需要记录的事件。"""
        t = msg.get("type")
        if t == "stats":
            self.log("stats", online=msg.get("online"))
        elif t == "card":
            c = msg["card"]
            self.log("card", id=c["id"], kind=c["kind"], risk=c["risk"], manual=c.get("manual", False),
                     latency_ms=c.get("latency_ms"), comments=[x["id"] for x in c.get("comments", [])])
        elif t == "source" and msg.get("state") != self.last_source_state:
            self.last_source_state = msg.get("state")
            self.log("source", state=msg.get("state"), note=msg.get("note", ""))

    def reading(self, paragraph_id):
        if paragraph_id and paragraph_id != self.last_reading:
            self.last_reading = paragraph_id
            self.log("reading", p=paragraph_id)

    def close(self):
        self.log("end")
        self.f.close()
        print(f"本场记录：{self.dir.relative_to(ROOT)}，复盘：.venv/bin/python -m server.report latest")
