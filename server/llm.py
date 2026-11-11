"""按 config/llm.yaml 的路由调用模型（OpenAI 兼容接口），返回解析后的 JSON。"""
import json
import os
import re
import time

from openai import AsyncOpenAI

from .config import route

FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


class LLMError(RuntimeError):
    pass


class JsonLLM:
    def __init__(self, route_name: str, timeout: float = 300):
        self.provider, self.cfg = route(route_name)
        key = os.environ.get(self.cfg["api_key_env"], "")
        if not key:
            raise LLMError(f"{self.cfg['api_key_env']} 未设置，请在项目根目录的 .env 中填写")
        self.client = AsyncOpenAI(api_key=key, base_url=self.cfg["base_url"], max_retries=1,
                                  timeout=timeout)
        self.usage = {"calls": 0, "prompt": 0, "cached": 0, "completion": 0, "seconds": 0.0}

    async def json(self, system: str, user: str, retries: int = 1) -> dict:
        """调用一次，要求输出 JSON；解析失败时重试。"""
        kwargs = {"model": self.cfg["model"],
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]}
        if self.cfg.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        if self.cfg.get("extra_body"):
            kwargs["extra_body"] = self.cfg["extra_body"]
        last = None
        for _ in range(retries + 1):
            t0 = time.perf_counter()
            resp = await self.client.chat.completions.create(**kwargs)
            self._account(resp, time.perf_counter() - t0)
            choice = resp.choices[0]
            text = (choice.message.content or "").strip()
            m = FENCE.match(text)
            try:
                return json.loads(m.group(1) if m else text)
            except json.JSONDecodeError as e:
                last = f"{e}; finish_reason={choice.finish_reason}"
        raise LLMError(f"模型输出不是合法 JSON：{last}")

    def _account(self, resp, seconds):
        u = resp.usage.model_dump() if resp.usage else {}
        self.usage["calls"] += 1
        self.usage["prompt"] += u.get("prompt_tokens") or 0
        self.usage["completion"] += u.get("completion_tokens") or 0
        self.usage["cached"] += (u.get("prompt_cache_hit_tokens")
                                 or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        self.usage["seconds"] += seconds

    def cost_yuan(self) -> float:
        p = self.cfg.get("price") or {}
        if not p:
            return 0.0
        u = self.usage
        return ((u["cached"] * p["input_cache_hit"] + (u["prompt"] - u["cached"]) * p["input_cache_miss"]
                 + u["completion"] * p["output"]) / 1e6)
