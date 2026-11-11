"""评论采集子进程：包装 DouyinLiveWebFetcher，把直播间事件以 JSON Lines 写到 stdout。

不修改 vendor 里的原库，只通过子类覆盖回调。
采集库自身的 print（心跳等）全部改到 stderr，stdout 只输出事件。

退出码：0 = 直播已结束；1 = 连接断开，可重连；2 = 启动失败（如取不到 room_id）。

  python server/sources/webfetcher_worker.py --live-id 123456789
  python server/sources/webfetcher_worker.py --mock-seconds 5      # 离线模拟，用于测试
"""
import argparse
import gzip
import hashlib
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

OUT = sys.stdout
sys.stdout = sys.stderr  # 采集库到处 print，统一改到 stderr

VENDOR = Path(__file__).resolve().parents[2] / "vendor" / "DouyinLiveWebFetcher"
SALT = os.environ.get("COPILOT_UID_SALT", "local-dev-salt")
_lock = threading.Lock()


def emit(type_, **fields):
    rec = {"t": round(time.time(), 3), "type": type_, **fields}
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        try:
            OUT.write(line)
            OUT.flush()
        except BrokenPipeError:
            os._exit(1)


def uid_hash(uid) -> str:
    """用户 ID 只保留加盐哈希，不落原值。"""
    return hashlib.sha256(f"{SALT}:{uid}".encode()).hexdigest()[:12]


def run_live(live_id: str) -> int:
    sys.path.insert(0, str(VENDOR))
    os.chdir(VENDOR)  # 原库用相对路径读取 sign.js / a_bogus.js

    import websocket
    from liveMan import DouyinLiveWebFetcher
    from protobuf.douyin import (
        ChatMessage,
        ControlMessage,
        EmojiChatMessage,
        GiftMessage,
        LikeMessage,
        PushFrame,
        Response,
        RoomUserSeqMessage,
    )

    push = {"now": None}  # 当前这一帧的服务器推送时间（Response.now）

    def server_fields(m):
        # 用于计算采集延迟和去重。实测聊天消息的 common.create_time 为 0，
        # 所以同时记 event_time 和 msg_id（其高 32 位是生成时的 Unix 秒）。
        c = m.common
        return {"server_t": c.create_time or None,
                "event_t": getattr(m, "event_time", 0) or None,
                "push_t": push["now"],
                "msg_id": str(c.msg_id) if c.msg_id else None}

    def on_chat(payload):
        m = ChatMessage().parse(payload)
        emit("chat", uid=uid_hash(m.user.id), nick=m.user.nick_name, content=m.content,
             **server_fields(m))

    def on_emoji_chat(payload):
        m = EmojiChatMessage().parse(payload)
        emit("chat", uid=uid_hash(m.user.id), nick=m.user.nick_name,
             content=m.default_content, emoji=True, **server_fields(m))

    def on_stats(payload):
        m = RoomUserSeqMessage().parse(payload)
        emit("stats", online=m.total, total_pv=m.total_pv_for_anchor)

    def on_gift(payload):
        m = GiftMessage().parse(payload)
        emit("gift", nick=m.user.nick_name, gift=m.gift.name, combo=m.combo_count)

    def on_like(payload):
        m = LikeMessage().parse(payload)
        emit("like", count=m.count)

    handlers = {
        "WebcastChatMessage": on_chat,
        "WebcastEmojiChatMessage": on_emoji_chat,
        "WebcastRoomUserSeqMessage": on_stats,
        "WebcastGiftMessage": on_gift,
        "WebcastLikeMessage": on_like,
    }

    class Fetcher(DouyinLiveWebFetcher):
        ended = False
        frames = 0
        methods = {}

        def _report_alive(self):
            # 每 30 秒报告一次收到的帧数和消息类型，用来判断连接是否还活着。
            # 小直播间里在线人数消息很少，不能靠它判断。
            while True:
                time.sleep(30)
                emit("alive", frames=self.frames, methods=dict(self.methods))
                self.methods = {}

        def _wsOnOpen(self, ws):
            emit("open", room_id=self.room_id)
            threading.Thread(target=self._report_alive, daemon=True).start()
            super()._wsOnOpen(ws)  # 启动原库的心跳线程

        def _wsOnMessage(self, ws, message):
            # 与原库逻辑相同，区别是解析错误会上报，而不是被 except: pass 吞掉
            self.frames += 1
            try:
                package = PushFrame().parse(message)
                response = Response().parse(gzip.decompress(package.payload))
            except Exception as e:
                emit("parse_error", stage="frame", error=repr(e)[:200])
                return
            push["now"] = response.now or None
            if response.need_ack:
                ack = PushFrame(log_id=package.log_id, payload_type="ack",
                                payload=response.internal_ext.encode("utf-8")).SerializeToString()
                ws.send(ack, websocket.ABNF.OPCODE_BINARY)
            for msg in response.messages_list:
                short = msg.method.removeprefix("Webcast")
                self.methods[short] = self.methods.get(short, 0) + 1
                if msg.method == "WebcastControlMessage":
                    try:
                        if ControlMessage().parse(msg.payload).status == 3:
                            Fetcher.ended = True
                            emit("ended")
                            self.stop()
                    except Exception as e:
                        emit("parse_error", stage=msg.method, error=repr(e)[:200])
                    continue
                handler = handlers.get(msg.method)
                if handler is None:
                    continue
                try:
                    handler(msg.payload)
                except Exception as e:
                    emit("parse_error", stage=msg.method, error=repr(e)[:200])

        def _wsOnError(self, ws, error):
            emit("ws_error", error=repr(error)[:200])

        def _wsOnClose(self, ws, *args):
            # 原库在这里调用已失效的 get_room_status，这里不调用
            emit("closed", detail=[str(a)[:100] for a in args])

    f = Fetcher(live_id)
    try:
        room_id = f.room_id  # 原库取不到时会抛 AttributeError
    except Exception as e:
        emit("fatal", reason="room_id", error=repr(e)[:200])
        return 2
    if not room_id:
        emit("fatal", reason="room_id", error="empty")
        return 2
    emit("starting", room_id=room_id)
    try:
        f.start()  # 阻塞直到连接关闭
    except Exception as e:
        emit("ws_error", error=repr(e)[:200])
    return 0 if Fetcher.ended else 1


def run_mock(seconds: float) -> int:
    rnd = random.Random()
    samples = ["真的假的？", "1000块怎么够啊", "我也是这样过来的", "主播哪个学校的",
               "奖学金好拿吗", "哈哈哈哈", "编的吧", "求更新"]
    emit("open", room_id="mock")
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(rnd.uniform(0.2, 0.8))
        emit("chat", uid=uid_hash(rnd.randint(1, 50)), nick=f"观众{rnd.randint(1, 50)}",
             content=rnd.choice(samples),
             server_t=int((time.time() - rnd.uniform(0.5, 2.0)) * 1000))  # 模拟 0.5～2 秒延迟
        if rnd.random() < 0.2:
            emit("stats", online=rnd.randint(100, 300), total_pv="1.2万")
    emit("closed", detail=["mock"])
    return 1


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live-id", help="直播间网址 live.douyin.com/<这串数字>")
    g.add_argument("--mock-seconds", type=float, help="离线模拟若干秒后退出")
    args = ap.parse_args()
    code = run_mock(args.mock_seconds) if args.mock_seconds else run_live(args.live_id)
    OUT.flush()
    os._exit(code)  # 原库的心跳线程不是守护线程，直接退出，避免进程挂住


if __name__ == "__main__":
    main()
