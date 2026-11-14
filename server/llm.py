"""按 config/llm.yaml 的路由调用模型（OpenAI 兼容接口），返回解析后的 JSON。"""
import asyncio
import json
import re
import socket
import ssl
import time
from contextlib import contextmanager

from httpx import ProxyError
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, DefaultAsyncHttpxClient

from .config import model_key, route

FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


class LLMError(RuntimeError):
    pass


def model_client(cfg: dict, key: str, *, timeout: float, max_retries: int) -> AsyncOpenAI:
    # 兼容端点只访问明确指定的地址，避免重定向把密钥或请求内容带到另一个位置。
    transport = DefaultAsyncHttpxClient(follow_redirects=False) if cfg.get("compatible") else None
    return AsyncOpenAI(api_key=key, base_url=cfg["base_url"], max_retries=max_retries, timeout=timeout,
                       http_client=transport)


class JsonLLM:
    def __init__(self, route_name: str, timeout: float = 300, *, selection: tuple[str, dict] | None = None,
                 api_key: str | None = None, max_retries: int = 1):
        try:
            self.provider, self.cfg = selection or route(route_name)
        except KeyError as e:
            raise LLMError("找不到所选模型，请检查 config/llm.yaml") from e
        if not self.cfg.get("model"):
            raise LLMError(f"{self.provider} 尚未设置模型 ID，请检查 config/llm.yaml")
        key = model_key(self.cfg["api_key_env"]) if api_key is None else api_key
        if not key:
            raise LLMError(f"{self.cfg['api_key_env']} 未设置，请在工作台「模型设置」填写")
        self.client = model_client(self.cfg, key, timeout=timeout, max_retries=max_retries)
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
            if not getattr(resp, "choices", None):
                raise LLMError("模型接口未返回有效的生成结果")
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


CONNECTION_TEST_TIMEOUT = 25
STATUS_HINTS = {
    301: "API 地址发生重定向，请填写服务商提供的最终 API 地址。",
    302: "API 地址发生重定向，请填写服务商提供的最终 API 地址。",
    307: "API 地址发生重定向，请填写服务商提供的最终 API 地址。",
    308: "API 地址发生重定向，请填写服务商提供的最终 API 地址。",
    400: "服务商拒绝了请求参数。请核对模型 ID，以及模型是否支持对话生成和 JSON 输出。",
    401: "API 密钥无效、已失效，或与所选服务不匹配。",
    402: "服务商账户余额不足，请检查账单。",
    403: "访问被拒绝，请检查项目权限、模型访问权限和服务地区。",
    404: "找不到模型或对话接口，请核对模型 ID、项目访问权限和服务预设。",
    408: "服务商请求超时，请重试。",
    422: "服务商无法处理请求，请核对模型 ID 和模型支持的参数。",
    429: "请求达到服务商限速，请稍后重试。",
    503: "服务商暂时不可用或繁忙，请稍后重试。",
    504: "服务商网关超时，请稍后重试。",
}
CODE_HINTS = {
    "insufficient_quota": "账户或项目额度不足，请检查服务商账单与用量上限。",
    "billing_hard_limit_reached": "用量已达到计费上限，请检查服务商账单与用量上限。",
    "organization_usage_limit_exceeded": "组织用量已达到上限，请查看服务商的用量设置。",
    "model_not_found": "模型 ID 不存在，或当前项目没有访问权限。",
    "unsupported_country_region_territory": "服务商不支持当前访问地区。",
}


def _connection_hint(error: APIConnectionError) -> str:
    # SDK 会包装底层连接错误。只识别异常类型，避免回显 URL、代理凭据或密钥。
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, ProxyError):
            return "代理连接失败，请检查本机代理配置。"
        if isinstance(cause, ssl.SSLError):
            return "TLS 证书校验或握手失败，请检查本机时间和网络证书。"
        if isinstance(cause, socket.gaierror):
            return "无法解析服务商域名，请检查 DNS 和网络。"
        cause = cause.__cause__
    return "无法连接服务商，请检查网络、代理或服务商状态。"


@contextmanager
def _provider_errors(timeout: int, *, listing=False):
    try:
        yield
    except TimeoutError:
        raise LLMError(f"等待服务商超过 {timeout} 秒，请稍后重试。") from None
    except APIStatusError as e:
        # 上游报错正文可能包含密钥，只显示已识别的错误类型和 HTTP 状态码。
        hint = CODE_HINTS.get(e.code) if isinstance(e.code, str) else None
        if listing and e.status_code in {404, 405, 501}:
            hint = "服务商未提供兼容的模型列表接口，可手动填写模型 ID。"
        elif not hint:
            fallback = "服务商暂时出现故障，请稍后重试。" if e.status_code >= 500 else "服务商拒绝了请求。"
            hint = STATUS_HINTS.get(e.status_code, fallback)
        raise LLMError(f"{hint}（HTTP {e.status_code}）") from None
    except APIConnectionError as e:
        hint = "等待服务商响应超时，请稍后重试。" if isinstance(e, APITimeoutError) else _connection_hint(e)
        raise LLMError(hint) from None
    except APIError:
        message = "服务商未返回有效的模型列表。" if listing else "服务商返回了无法读取的对话响应。"
        raise LLMError(message) from None


async def probe_model(selection: tuple[str, dict], key: str) -> int:
    """使用与实际生成相同的请求格式测试，返回毫秒耗时。"""
    start = time.perf_counter()
    # 只发送固定的测试提示，不带稿件、档案或评论，也不重试付费请求。
    llm = JsonLLM("script", timeout=CONNECTION_TEST_TIMEOUT, selection=selection, api_key=key, max_retries=0)
    try:
        with _provider_errors(CONNECTION_TEST_TIMEOUT):
            try:
                async with asyncio.timeout(CONNECTION_TEST_TIMEOUT):
                    result = await llm.json("这是连接测试。只输出 JSON 对象：{\"ok\":true}。", "返回该 JSON。", retries=0)
            except LLMError:
                result = None
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise LLMError("服务商已响应，但未返回预期的 JSON，请检查模型的 JSON 输出支持。")
    finally:
        await llm.client.close()
    return round((time.perf_counter() - start) * 1000)


async def list_provider_models(cfg: dict, key: str) -> list[str]:
    """按当前凭据读取服务商公布的模型 ID，不调用生成接口。"""
    timeout = 12
    with _provider_errors(timeout, listing=True):
        async with model_client(cfg, key, max_retries=0, timeout=timeout) as client:
            async with asyncio.timeout(timeout):
                page = await client.models.list()
    # 某些代理会把 HTML 当作成功响应，不能把它解释成空模型列表。
    if not isinstance(getattr(page, "data", None), list):
        raise LLMError("服务商未返回有效的模型列表，可手动填写模型 ID。")
    return sorted({item.id for item in page.data if item and isinstance(getattr(item, "id", None), str)})
