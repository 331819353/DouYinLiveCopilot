"""实时回复引擎：攒一批评论，结合主播眼前的文案调用模型，校验后推送卡片。

模型输出一律经过程序校验：引用的评论必须在这一批里，引用的文案必须逐字出自
当前可视段落；求助类卡片只保留给主播的提醒，并附上固定的求助资源。
"""
import asyncio
import itertools
import json
import os
import time
from collections import deque

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ..config import HELP_RESOURCES, PROMPTS, route
from ..profile import load_profile, persona_text
from .schema import parse_output

# 每轮从候选评论里挑最多 PICK 条，每条单独、并行调用一次模型。
# 一次送一批会让输出变长：实测 5 条一批要 16.7 秒；单条调用在 M0 比选中 p95 为 4.6 秒。
PICK = 3           # 每轮最多回应 3 条
INTERVAL = 10      # 常规每 10 秒一轮
QUICK = 3          # 有提问时最快 3 秒
PER_MIN = 6        # 每分钟最多 6 轮
POOL_TTL = 60      # 评论 60 秒内没被处理就过期
CANARY = "CANARY-7Q2"  # 系统提示词里的内部编号，出现在输出里说明发生了泄漏


class ReplyEngine:
    def __init__(self, bus, script, enabled: bool = True):
        self.bus = bus
        self.script = script
        self.provider, self.cfg = route("reply")
        key = os.environ.get(self.cfg["api_key_env"], "")
        self.enabled = enabled and bool(key) and bool(self.cfg.get("model"))
        if not enabled:
            self.disabled_reason = "已用 --no-reply 关闭"
        elif not key:
            self.disabled_reason = f"{self.cfg['api_key_env']} 未设置，请填写 .env"
        else:
            self.disabled_reason = None
        self.client = AsyncOpenAI(api_key=key, base_url=self.cfg["base_url"], max_retries=0,
                                  timeout=20) if self.enabled else None
        profile, self.demo_profile = load_profile()
        system = ((PROMPTS / "reply_system.md").read_text("utf-8")
                  .replace("{name}", profile.get("name", "主播"))
                  .replace("{persona}", persona_text(profile, with_materials=True)))
        self.system = f"{system}\n\n# 节目圣经（全场固定）\n\n{script.bible}"  # 固定前缀，命中缓存

        self.pool: deque = deque()     # 待处理的评论
        self.comments: dict = {}       # id -> 评论，供手动触发和卡片回显
        self.carded: set = set()       # 已经出过卡片的评论
        self.visible: list = []
        self.reading = None
        self.calls: deque = deque()    # 最近一分钟的自动调用时间
        self.inflight = 0
        self.last_call = 0.0
        self.latencies: deque = deque(maxlen=50)
        self.card_seq = itertools.count(1)
        self.n = {"calls": 0, "cards": 0, "dropped": 0, "errors": 0}

    # ---------- 输入 ----------
    def remember(self, c: dict):
        self.comments[c["id"]] = c
        if len(self.comments) > 2000:
            for k in list(self.comments)[:500]:
                self.comments.pop(k, None)

    def add(self, c: dict):
        self.pool.append(c)

    def set_viewport(self, visible: list, reading):
        self.visible = [v for v in visible if v in self.script.paragraphs]
        self.reading = reading if reading in self.script.paragraphs else None

    async def trigger_now(self, comment_id):
        c = self.comments.get(comment_id)
        if not c:
            return
        if not self.enabled:
            await self._status("disabled")
            return
        self.carded.discard(comment_id)  # 主播主动要求，允许重新生成
        asyncio.create_task(self._call([c], manual=True))

    # ---------- 调度 ----------
    async def run(self):
        await self._status("idle" if self.enabled else "disabled")
        while True:
            await asyncio.sleep(1)
            if not self.enabled or self.inflight or not self.visible:
                continue
            now = time.time()
            while self.pool and now - self.pool[0]["t"] > POOL_TTL:
                self.pool.popleft()
            if not self.pool:
                continue
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= PER_MIN:
                continue
            has_q = any(c["q"] for c in self.pool)
            gap = QUICK if has_q else INTERVAL
            settle = 1 if has_q else 3  # 稍等一下，让同一波评论攒到一起
            if now - self.last_call < gap or now - self.pool[-1]["t"] < settle:
                continue
            picks = self._pick(list(self.pool))
            self.pool.clear()
            self.calls.append(now)
            self.last_call = now
            for c in picks:
                asyncio.create_task(self._call([c]))

    @staticmethod
    def _pick(pool):
        """挑出最值得回应的几条：提问优先，其次内容较长、较新；相同内容只留一条。"""
        seen, uniq = set(), []
        for c in reversed(pool):  # 新的在前
            if c["text"] not in seen:
                seen.add(c["text"])
                uniq.append(c)
        uniq.sort(key=lambda c: (not c["q"], -min(len(c["text"]), 30)))
        return uniq[:PICK]

    # ---------- 调用与校验 ----------
    def _messages(self, batch, visible):
        paras = self.script.paragraphs
        vis = "\n".join(f'<p id="{pid}">{paras[pid]["text"]}</p>' for pid in visible)
        comments = json.dumps([{"id": f"c_{i + 1}", "text": c["text"]} for i, c in enumerate(batch)],
                              ensure_ascii=False)
        user = (f"<visible_script>\n{vis}\n</visible_script>\n"
                f"<next_summary>{self.script.next_summary(self.reading)}</next_summary>\n"
                f"<untrusted_comments>\n{comments}\n</untrusted_comments>\n"
                "请按系统说明输出 json。")
        return [{"role": "system", "content": self.system}, {"role": "user", "content": user}]

    async def _call(self, batch, manual=False):
        self.inflight += 1
        self.last_call = time.time()
        self.n["calls"] += 1
        await self._status("busy")
        visible = list(self.visible)
        idmap = {f"c_{i + 1}": c for i, c in enumerate(batch)}
        kwargs = {"model": self.cfg["model"], "messages": self._messages(batch, visible)}
        if self.cfg.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        if self.cfg.get("extra_body"):
            kwargs["extra_body"] = self.cfg["extra_body"]
        t0 = time.perf_counter()
        err = None
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except APITimeoutError:
            err = "模型调用超时，已跳过这一批"
        except APIConnectionError:
            err = "网络错误，已跳过这一批"
        except APIStatusError as e:
            err = f"模型接口返回 {e.status_code}" + ("，可能被内容审核拦截" if e.status_code == 400 else "")
        finally:
            self.inflight -= 1
        latency = int((time.perf_counter() - t0) * 1000)
        if err:
            self.n["errors"] += 1
            await self._status("error", err)
            return
        self.latencies.append(latency)
        text = resp.choices[0].message.content or ""
        if CANARY in text:
            self.n["dropped"] += 1
            await self._status("error", "输出包含系统提示词内容，整批丢弃")
            return
        out = parse_output(text)
        if out is None:
            self.n["errors"] += 1
            await self._status("error", "模型输出格式不合法，已跳过这一批")
            return
        for card in out.cards:
            d = self._accept(card, idmap, visible, latency, manual)
            if d is None:
                self.n["dropped"] += 1
                continue
            self.n["cards"] += 1
            await self.bus.publish({"type": "card", "card": d})
        await self._status("idle" if not self.inflight else "busy", f"上一条用时 {latency / 1000:.1f} 秒")

    def _accept(self, card, idmap, visible, latency, manual):
        cs = [idmap[i] for i in card.comment_ids if i in idmap]
        cs = [c for c in cs if c["id"] not in self.carded]
        if not cs:
            return None
        d = card.model_dump()
        if card.risk == "escalate":
            # 求助类：不显示模型生成的展开、提问和拉回话术，附上固定的求助资源
            d.update(expand="", discussion_question="", bridge_back="", help=HELP_RESOURCES)
        else:
            p = self.script.paragraphs.get(card.anchor_paragraph_id)
            if (card.anchor_paragraph_id not in visible or p is None
                    or len(card.anchor_quote) < 6 or card.anchor_quote not in p["text"]):
                return None
        for c in cs:
            self.carded.add(c["id"])
        d.pop("comment_ids")
        d.update(id=f"k{next(self.card_seq)}", t=time.time(), latency_ms=latency, manual=manual,
                 comments=[{"id": c["id"], "nick": c.get("nick", ""), "text": c["text"]} for c in cs])
        return d

    async def _status(self, state, note=None):
        lat = sorted(self.latencies)
        await self.bus.publish({
            "type": "reply_status", "state": state, "provider": self.provider,
            "note": note or (self.disabled_reason if state == "disabled" else ""),
            "p50_ms": lat[len(lat) // 2] if lat else None, **self.n})
