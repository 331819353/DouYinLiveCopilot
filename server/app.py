"""本机服务：提供三栏界面，通过 WebSocket 推送评论、回复卡片和状态。

  .venv/bin/python -m server.app --mock                  # 模拟评论
  .venv/bin/python -m server.app --live-id 你的房间号     # 真实直播间
  .venv/bin/python -m server.app --manual                # 只手动粘贴评论
  .venv/bin/python -m server.app --review                # 只打开文案审定页

然后用浏览器打开 http://127.0.0.1:8765 ，全屏使用。
"""
import argparse
import asyncio
import itertools
import json
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .bus import Bus
from .config import ROOT, STATIC, filter_words
from .reply.engine import ReplyEngine
from .reply.filters import CommentFilter, is_question
from .review_api import router as review_router
from .script_store import Script
from .session import SessionRecorder
from .sources.supervisor import SourceSupervisor

COMMENT_FIELDS = ("id", "nick", "text", "t", "q", "filtered", "source")
CARD_ACTIONS = {"expand", "collapse", "used", "pin", "unpin", "dismiss", "reply_now"}


def resolve_script(arg: str):
    """--script latest 表示 data/scripts/ 下最新生成的一份。"""
    if arg == "latest":
        found = sorted((ROOT / "data" / "scripts").glob("*.json"))
        if not found:
            raise SystemExit("data/scripts/ 下还没有生成的文案，先运行 python -m server.script")
        print(f"加载文案：{found[-1].relative_to(ROOT)}")
        return found[-1]
    return ROOT / arg


def build_app(args) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(review_router)

    @app.get("/review")
    async def review_page():
        return FileResponse(STATIC / "review.html")

    if args.review:  # 只审定文案：不采集、不调用模型、不录制
        @app.get("/")
        async def root():
            return RedirectResponse("/review")
        return app

    bus = Bus()
    script_path = resolve_script(args.script)
    script = Script(script_path)
    if not script.data.get("approved_at"):
        print("提示：这份文案还没有审定，可在 /review 页面审定。")
    filt = CommentFilter(filter_words())
    engine = ReplyEngine(bus, script, enabled=not args.no_reply)
    mode = "live" if args.live_id else "mock" if args.mock is not None else "manual"
    rec = None if args.no_record else SessionRecorder(script.data, script_path, mode)
    if rec:
        bus.taps.append(rec.tap)
    seq = itertools.count(1)
    started = time.time()

    async def ingest(uid, nick, text, t=None, source="live"):
        reason = filt.check(uid, text)
        c = {"id": f"m{next(seq)}", "uid": uid, "nick": nick, "text": text,
             "t": t or time.time(), "q": is_question(text), "filtered": reason, "source": source}
        engine.remember(c)
        if rec:
            rec.comment(c)
        if reason is None:
            engine.add(c)
        await bus.publish({"type": "comment", **{k: c[k] for k in COMMENT_FIELDS}})

    async def on_event(ev):
        typ = ev.get("type")
        if typ == "chat" and ev.get("content"):
            await ingest(ev.get("uid", ""), ev.get("nick", ""), ev["content"], ev.get("t"),
                         source=sup.kind)
        elif typ == "stats":
            await bus.publish({"type": "stats", "online": ev.get("online"),
                               "total_pv": ev.get("total_pv")})

    sup = None
    if args.live_id:
        sup = SourceSupervisor(on_event, live_id=args.live_id)
    elif args.mock is not None:
        sup = SourceSupervisor(on_event, mock_file=str(ROOT / args.mock) if args.mock else "")

    async def health_loop():
        while True:
            await bus.publish(sup.health() if sup else {
                "type": "source", "kind": "manual", "state": "manual",
                "note": "手动模式：在评论区下方粘贴评论", "restarts": 0})
            await asyncio.sleep(3)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(engine.run()), asyncio.create_task(health_loop())]
        if sup:
            tasks.append(asyncio.create_task(sup.run()))
        yield
        if sup:
            await sup.stop()
        for t in tasks:
            t.cancel()
        if rec:
            rec.close()

    app.router.lifespan_context = lifespan

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_text(json.dumps({
            "type": "hello", "script": script.to_client(), "started": started,
            "approved": bool(script.data.get("approved_at")),
            "script_file": script_path.name}, ensure_ascii=False))
        for m in bus.snapshot():
            await sock.send_text(json.dumps(m, ensure_ascii=False))
        bus.clients.add(sock)
        try:
            while True:
                try:
                    msg = json.loads(await sock.receive_text())
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == "viewport":
                    engine.set_viewport(msg.get("visible") or [], msg.get("reading"))
                    if rec and engine.reading:
                        rec.reading(engine.reading)
                elif t == "manual_comment":
                    text = str(msg.get("text", "")).strip()[:200]
                    if text:
                        await ingest("manual", "手动输入", text, source="manual")
                elif t == "reply_now":
                    if rec:
                        rec.log("card_action", action="reply_now", comment=msg.get("comment_id"))
                    await engine.trigger_now(msg.get("comment_id"))
                elif t == "card_action" and msg.get("action") in CARD_ACTIONS:
                    if rec:
                        rec.log("card_action", id=str(msg.get("id", ""))[:20], action=msg["action"])
        except WebSocketDisconnect:
            pass
        finally:
            bus.clients.discard(sock)

    return app


def main():
    ap = argparse.ArgumentParser(description="直播提词与评论互动助手")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live-id", help="直播间网址 live.douyin.com/<这串数字>，只连自己的直播间")
    g.add_argument("--mock", nargs="?", const="examples/mock_comments.txt",
                   help="模拟评论，可指定样例文件（默认 examples/mock_comments.txt）")
    g.add_argument("--manual", action="store_true", help="不采集，只手动粘贴评论")
    g.add_argument("--review", action="store_true", help="只打开文案审定页")
    ap.add_argument("--script", default="examples/script_demo.json",
                    help="直播文案 JSON；latest 表示最新生成的一份")
    ap.add_argument("--no-reply", action="store_true", help="关闭 AI 回复")
    ap.add_argument("--no-record", action="store_true", help="不录制本场数据")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    page = "/review" if args.review else ""
    print(f"打开 http://127.0.0.1:{args.port}{page} ，按 Ctrl-C 停止。")
    uvicorn.run(build_app(args), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
