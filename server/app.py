"""本机服务：提供三栏界面，通过 WebSocket 推送评论、回复卡片和状态。

  .venv/bin/python -m server.app --mock                  # 模拟评论
  .venv/bin/python -m server.app                         # 页面填房间号
  .venv/bin/python -m server.app --live-id 你的房间号     # 真实直播间
  .venv/bin/python -m server.app --manual                # 只手动粘贴评论
  .venv/bin/python -m server.app --review                # 只打开文案审定页

然后用浏览器打开 http://127.0.0.1:8765 ，全屏使用。
"""
import argparse
import asyncio
import itertools
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from hashlib import sha256
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from httpx import InvalidURL, URL
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .bus import Bus
from .config import (
    COMPATIBLE_ID_PREFIX, COMPATIBLE_PROVIDER, ROOT, STATIC, base_llm_config, compatible_model_config, filter_words,
    load_llm_config, local_model_settings, model_key, save_model_settings,
)
from .insert import InsertEngine
from .llm import LLMError, list_provider_models, probe_model
from .reply.engine import ReplyEngine
from .reply.filters import CommentFilter, is_question
from .review_api import _path as review_script_path, router as review_router
from .script.jobs import ChapterAction, ScriptJobs, ScriptOptions
from .script_store import Script
from .session import SessionRecorder
from .sources.supervisor import LIVE_FETCHER, SourceSupervisor

COMMENT_FIELDS = ("id", "nick", "text", "t", "q", "filtered", "source")
CARD_ACTIONS = {"expand", "collapse", "used", "pin", "unpin", "dismiss", "reply_now"}
ROOM = re.compile(r"([0-9]{1,30})|https?://live\.douyin\.com/([0-9]{1,30})/?(?:[?#][^\s]*)?", re.ASCII)
MODEL_ID = re.compile(r"[A-Za-z0-9._:/-]{1,150}")
API_KEY = re.compile(r"[!-~]{1,4096}")
SERVICE_NAMES = {"DEEPSEEK_API_KEY": "DeepSeek", "OPENAI_API_KEY": "OpenAI", "ARK_API_KEY": "火山方舟"}
COMPATIBLE_SERVICE = "OpenAI API 兼容"
logger = logging.getLogger(__name__)


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def frontend_page(name: str):
    # HTML 与所有前端资源共用一个内容版本。版本放在目录里，JS 相对导入也走同一版本。
    digest = sha256()
    for asset in sorted(STATIC.iterdir()):
        if asset.suffix in {".css", ".js"} and asset.is_file():
            digest.update(asset.name.encode("utf-8") + b"\0" + asset.read_bytes() + b"\0")
    html = (STATIC / name).read_text("utf-8")
    base = f'/ui/{digest.hexdigest()[:16]}/'
    return HTMLResponse(html.replace('"/static/', f'"{base}'), headers={"Cache-Control": "no-store"})


class RoomSelection(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    room: str | None = Field(max_length=300)


class ScriptSelection(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class ModelRoutes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script: str
    reply: str


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(max_length=100)
    api_key: SecretStr | None = None


class ModelAddition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(max_length=100)
    model: str = Field(max_length=150)
    draft_id: str = Field(default="", pattern=r"^(?:draft-[0-9]{1,8})?$")


class ModelConnectionTest(ProviderRequest):
    model: str = Field(max_length=150)
    base_url: str | None = Field(default=None, max_length=2048)


class CompatibleModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str | None = Field(default=None, pattern=rf"^{COMPATIBLE_ID_PREFIX}[0-9a-f]{{24}}$")
    base_url: str = Field(max_length=2048)
    model: str = Field(max_length=150)
    api_key: SecretStr | None = None


class ModelSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routes: ModelRoutes | None = None
    models: dict[str, str] = Field(default_factory=dict, max_length=30)
    keys: dict[str, SecretStr] = Field(default_factory=dict, max_length=30)
    additions: list[ModelAddition] = Field(default_factory=list, max_length=30)
    compatible: CompatibleModelUpdate | None = None


def model_entry_id(base, model):
    identity = (base["api_key_env"], base["base_url"], model)
    return "local-" + sha256("\0".join(identity).encode("utf-8")).hexdigest()[:24]


def compatible_base_url(value: str) -> str:
    # 地址只接收 HTTP(S) 服务端点，密钥应独立填写；兼容粘贴完整的 Chat Completions 地址。
    detail = "请填写有效的 HTTP 或 HTTPS API 地址，地址中不能包含账号、密码、查询参数或片段"
    if any(char.isspace() or ord(char) < 32 or char in "\\?#" for char in value):
        raise HTTPException(422, detail)
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                or (parts.port is not None and not 1 <= parts.port <= 65535)):
            raise ValueError
        endpoint = URL(value)
    except (ValueError, InvalidURL):
        raise HTTPException(422, detail) from None
    path = endpoint.raw_path.rstrip(b"/").removesuffix(b"/chat/completions").rstrip(b"/")
    return str(endpoint.copy_with(raw_path=path)).rstrip("/")


def compatible_selection(provider, address, model, settings):
    base_url = compatible_base_url(address)
    entry_id = COMPATIBLE_ID_PREFIX + sha256("\0".join((base_url, model)).encode("utf-8")).hexdigest()[:24]
    if provider != COMPATIBLE_PROVIDER and (
            provider != entry_id or provider not in settings.get("compatible", {})):
        raise HTTPException(422, "已保存模型的地址和模型 ID 固定，使用新地址或 ID 时请添加模型")
    return entry_id, compatible_model_config(entry_id, base_url, model)


async def read_model_request(request: Request, schema):
    # 保存与测试都可携带密钥，只接受本机同源 JSON，并且不回显校验失败的原始内容。
    origin = request.headers.get("origin")
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"} or (
            origin and origin != f"{request.url.scheme}://{request.url.netloc}"):
        raise HTTPException(403, "请在本机工作台页面操作模型设置")
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "模型设置需要 JSON 请求")
    try:
        return schema.model_validate(await request.json())
    except ValueError:
        raise HTTPException(422, "模型设置格式不正确") from None


def model_options(cfg, settings):
    # 密钥仅用于判断就绪状态。页面和任务快照都不返回密钥内容。
    added = settings.get("added", {})
    key_sources = {}
    for entry in cfg["providers"].values():
        name = entry["api_key_env"]
        if name not in key_sources:
            key_sources[name] = ("local" if settings.get("keys", {}).get(name)
                                 else "env" if model_key(name, settings) else None)
    providers = []
    for provider, entry in cfg["providers"].items():
        key_name = entry["api_key_env"]
        key_source = key_sources[key_name]
        option = {
            "id": provider, "label": entry.get("label", entry["model"] or provider),
            "model": entry["model"], "key_env": key_name,
            "service": SERVICE_NAMES.get(key_name, key_name),
            "base": added[provider]["base"] if provider in added else provider,
            "catalog_group": model_entry_id(entry, ""),
            "key_source": key_source, "has_key": bool(key_source), "ready": bool(entry["model"] and key_source),
        }
        if entry.get("compatible"):
            # 同名模型可能来自不同网关。下拉选项带地址，卡片单独显示地址。
            option.update(compatible=True, service=COMPATIBLE_SERVICE, base=COMPATIBLE_PROVIDER,
                          catalog_group=COMPATIBLE_PROVIDER, base_url=entry["base_url"],
                          label=f"{entry['model']} · {entry['base_url']}")
        providers.append(option)
    if not settings.get("compatible"):
        # 空卡片仅供发现入口，始终不进入运行时路由或默认模型的可选项。
        providers.append({
            "id": COMPATIBLE_PROVIDER, "catalog_group": COMPATIBLE_PROVIDER, "base": COMPATIBLE_PROVIDER,
            "compatible": True, "service": COMPATIBLE_SERVICE, "key_env": COMPATIBLE_PROVIDER,
            "label": COMPATIBLE_SERVICE, "model": "", "has_key": False, "ready": False, "key_source": None,
        })
    return {"default": cfg["routes"]["script"], "routes": cfg["routes"], "providers": providers}


def request_model_key(cfg, secret, settings):
    key = secret.get_secret_value().strip() if secret is not None else model_key(cfg["api_key_env"], settings)
    if not key:
        raise HTTPException(422, "请填写 API 密钥，或先为该服务保存密钥")
    if not API_KEY.fullmatch(key):
        raise HTTPException(422, "API 密钥过长或含无效字符")
    return key


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
    app.mount("/static", FrontendFiles(directory=STATIC), name="static")
    app.mount("/ui/{version}", FrontendFiles(directory=STATIC), name="frontend")
    app.include_router(review_router)

    @app.get("/review")
    async def review_page():
        return frontend_page("review.html")

    if args.review:  # 只审定文案：不采集、不调用模型、不录制
        @app.get("/")
        async def root():
            return RedirectResponse("/review")
        return app

    bus = Bus()
    script_path = resolve_script(args.script)
    initial_path = script_path
    script = Script(script_path)
    if not script.data.get("approved_at"):
        print("提示：这份文案还没有审定，可在 /review 页面审定。")
    filt = CommentFilter(filter_words())
    engine = ReplyEngine(bus, script, enabled=not args.no_reply)
    inserts = InsertEngine(bus, script, enabled=not args.no_reply)
    mode = "live" if args.live_id else "mock" if args.mock is not None else "manual"
    rec = None if args.no_record else SessionRecorder(script.data, script_path, mode, args.live_id)
    if rec:
        bus.taps.append(rec.tap)
    seq = itertools.count(1)
    service_started = started = time.time()
    room_rev = script_rev = 0
    workspace_lock = asyncio.Lock()  # 串行处理换房、换稿、模型设置以及操作当前文案的指令
    jobs = ScriptJobs()

    async def ingest(uid, nick, text, t=None, source="live"):
        reason = filt.check(uid, text)
        c = {"id": f"m{next(seq)}", "uid": uid, "nick": nick, "text": text,
             "t": t or time.time(), "q": is_question(text), "filtered": reason, "source": source}
        engine.remember(c)
        if rec:
            rec.comment(c)
        if reason is None:
            engine.add(c)
            inserts.add(c)
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
    source_task = None

    def source_health():
        return (sup.health() if sup else {"type": "source", "kind": "manual", "state": "manual",
                "note": "输入房间号接收直播评论，也可以手动输入测试评论。", "restarts": 0,
                "room": None}) | {"room_rev": room_rev}

    def script_info():
        return {"script": script.to_client(), "started": started, "script_rev": script_rev,
                "approved": bool(script.data.get("approved_at")), "script_file": script_path.name,
                "reviewable": script_path.parent.resolve() == (ROOT / "data" / "scripts").resolve()}

    def rotate_recording():
        nonlocal rec
        if rec is not None:
            bus.taps.remove(rec.tap)
            rec.close()
            rec = SessionRecorder(script.data, script_path, sup.kind if sup else "manual",
                                  sup.live_id if sup else None)
            bus.taps.append(rec.tap)

    async def stop_source():
        nonlocal source_task
        if sup and source_task:
            await sup.stop()
            await asyncio.gather(source_task, return_exceptions=True)
            source_task = None

    async def health_loop():
        while True:
            async with workspace_lock:
                await bus.publish(source_health())
            await asyncio.sleep(3)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal source_task
        tasks = [asyncio.create_task(engine.run()), asyncio.create_task(health_loop()),
                 asyncio.create_task(inserts.run())]
        source_task = asyncio.create_task(sup.run()) if sup else None
        try:
            yield
        finally:
            # 让采集监督器先退出并回收子进程，避免事件循环关闭后再处理管道。
            try:
                await stop_source()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await jobs.stop()
                await engine.reset()
                if engine.client:
                    await engine.client.close()
                await inserts.reset()
                if rec:
                    rec.close()

    app.router.lifespan_context = lifespan

    @app.get("/")
    async def index():
        return frontend_page("index.html")

    @app.get("/api/workbench/generation")
    async def generation():
        return jobs.snapshot()

    @app.get("/api/workbench/models")
    async def script_models():
        settings = local_model_settings()
        return model_options(load_llm_config(settings), settings)

    @app.put("/api/workbench/models")
    async def update_models(request: Request):
        update = await read_model_request(request, ModelSettingsUpdate)
        async with workspace_lock:
            settings = local_model_settings()
            providers = load_llm_config(settings)["providers"]
            # 卡片分别保存密钥和模型，只有默认模型表单可以覆盖现有路由。
            routes = update.routes.model_dump() if update.routes else dict(settings.get("routes", {}))
            if not update.models.keys() <= providers.keys():
                raise HTTPException(422, "请选择列表中的模型服务")
            key_names = {info["api_key_env"] for info in providers.values()}
            if not update.keys.keys() <= key_names:
                raise HTTPException(422, "请选择列表中的密钥类型")
            models = dict(settings.get("models", {}))
            for provider, value in update.models.items():
                if providers[provider].get("compatible"):
                    raise HTTPException(422, "请在兼容供应商卡片添加新模型 ID")
                value = value.strip()
                if not MODEL_ID.fullmatch(value):
                    raise HTTPException(422, "模型 ID 格式不正确")
                models[provider] = value
            keys = dict(settings.get("keys", {}))
            for name, secret in update.keys.items():
                value = secret.get_secret_value().strip()
                if not API_KEY.fullmatch(value):
                    raise HTTPException(422, "API 密钥为空、过长或含空白字符")
                keys[name] = value
            added = dict(settings.get("added", {}))
            bases = base_llm_config()["providers"]
            compatible = dict(settings.get("compatible", {}))
            updated = settings | {"routes": routes, "models": models, "keys": keys, "added": added,
                                  "compatible": compatible}
            if details := update.compatible:
                if not MODEL_ID.fullmatch(details.model):
                    raise HTTPException(422, "请填写有效的模型 ID")
                entry_id, entry = compatible_selection(details.id or COMPATIBLE_PROVIDER, details.base_url,
                                                       details.model, settings)
                if details.id is None and entry_id in compatible:
                    raise HTTPException(409, "这个地址和模型 ID 已保存，请展开模型列表并编辑该模型")
                if details.id is None and not details.api_key:
                    raise HTTPException(422, "请填写这个兼容模型的 API 密钥")
                keys[entry_id] = request_model_key(entry, details.api_key, settings)
                compatible[entry_id] = {"base_url": entry["base_url"], "model": entry["model"]}
            existing = {model_entry_id(p, p["model"]): provider
                        for provider, p in load_llm_config(updated)["providers"].items()}
            draft_ids = {}
            for addition in update.additions:
                base = bases.get(addition.provider)
                if base is None or not MODEL_ID.fullmatch(addition.model):
                    raise HTTPException(422, "新模型需要有效的服务预设与模型 ID")
                entry_id = model_entry_id(base, addition.model)
                if entry_id not in existing:
                    added[entry_id] = {"base": addition.provider, "model": addition.model}
                    existing[entry_id] = entry_id
                if addition.draft_id:
                    if addition.draft_id in draft_ids:
                        raise HTTPException(422, "新增模型的编号重复，请重新打开设置")
                    draft_ids[addition.draft_id] = existing[entry_id]
            # 新增模型可以在同一次保存中设为默认，页面用临时编号引用它。
            for name, provider in routes.items():
                routes[name] = draft_ids.get(provider, provider)
            cfg = load_llm_config(updated)
            if update.routes and not set(routes.values()) <= cfg["providers"].keys():
                raise HTTPException(422, "请选择清单中的默认模型")
            if update.routes and any(not cfg["providers"][provider]["model"] for provider in routes.values()):
                raise HTTPException(422, "请先填写所选默认模型的 ID")
            save_model_settings(updated)
            await engine.reload_model()
            inserts.refresh_models()
            await inserts.status()
            return model_options(cfg, updated)

    @app.post("/api/workbench/models/test")
    async def test_model(request: Request):
        test = await read_model_request(request, ModelConnectionTest)
        if not MODEL_ID.fullmatch(test.model):
            raise HTTPException(422, "请填写有效的模型 ID")
        settings = local_model_settings()
        if test.provider == COMPATIBLE_PROVIDER or test.base_url is not None:
            _, cfg = compatible_selection(test.provider, test.base_url or "", test.model, settings)
            # 测试未保存的新模型时必须明确提供密钥，避免同地址已有配置被隐式取用。
            if test.provider == COMPATIBLE_PROVIDER and not test.api_key:
                raise HTTPException(422, "请填写这个兼容模型的 API 密钥")
        else:
            # 内置预设仅在内存里覆盖模型 ID，保留与正式请求一致的参数规则。
            cfg = load_llm_config(settings)["providers"].get(test.provider)
            if cfg is not None and cfg["model"] != test.model:
                if cfg.get("compatible"):
                    raise HTTPException(422, "测试其他模型 ID 时请填写对应的 API 地址")
                models = {**settings.get("models", {}), test.provider: test.model}
                cfg = load_llm_config(settings | {"models": models})["providers"][test.provider]
        if cfg is None:
            raise HTTPException(422, "请选择列表中的模型服务")
        key = request_model_key(cfg, test.api_key, settings)
        try:
            elapsed = await probe_model((test.provider, cfg), key)
        except LLMError as e:
            raise HTTPException(502, str(e)) from None
        return {"elapsed_ms": elapsed}

    @app.post("/api/workbench/models/catalog")
    async def model_catalog(request: Request):
        selection = await read_model_request(request, ProviderRequest)
        settings = local_model_settings()
        cfg = load_llm_config(settings)["providers"].get(selection.provider)
        if cfg is None:
            raise HTTPException(422, "请选择列表中的模型服务")
        key = request_model_key(cfg, selection.api_key, settings)
        try:
            listing = await list_provider_models(cfg, key)
        except LLMError as e:
            raise HTTPException(502, str(e)) from None
        # 目录不保证支持对话生成。返回服务商模型 ID，是否可用由「测试连接」验证。
        return {"models": [model for model in listing if MODEL_ID.fullmatch(model)]}

    @app.post("/api/workbench/generation", status_code=202)
    async def generate_script(options: ScriptOptions):
        return jobs.start(options)

    @app.post("/api/workbench/generation/action")
    async def chapter_action(command: ChapterAction):
        return jobs.action(command)

    @app.put("/api/workbench/room")
    async def select_room(selection: RoomSelection):
        nonlocal sup, source_task, room_rev
        room = None
        if selection.room is not None:
            match = ROOM.fullmatch(selection.room)
            if not match:
                raise HTTPException(422, "请输入数字房间号或 live.douyin.com 的直播间链接")
            room = match[1] or match[2]
            if not LIVE_FETCHER.is_file():
                raise HTTPException(503, "评论采集库未安装，请按 README 的「获取评论采集库」完成安装")
        async with workspace_lock:
            if (room is None and sup is None) or (
                    sup and room and sup.live_id == room
                    and sup.health()["state"] in {"starting", "ok", "quiet"}):
                return source_health()
            # 等旧进程退出再清缓存、启新进程，杜绝旧房间仍向页面送评论。
            await stop_source()
            await asyncio.gather(engine.reset(clear_comments=True), inserts.reset(clear_comments=True))
            filt.recent.clear()
            sup = SourceSupervisor(on_event, live_id=room) if room else None
            room_rev += 1
            rotate_recording()
            state = source_health()
            await bus.publish(state, clear_comments=True, clear_cards=True)
            await engine._status("idle" if engine.enabled else "disabled")
            await inserts.status()
            if sup:
                source_task = asyncio.create_task(sup.run())
            return state

    @app.put("/api/workbench/script")
    async def select_script(selection: ScriptSelection):
        nonlocal script_path, started, script_rev
        path = initial_path if selection.name == "@initial" else review_script_path(selection.name)
        try:
            selected = Script(path)
            if not any(p["text"].strip() for p in selected.paragraphs.values()):
                raise ValueError("没有口播内容")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning("无法载入文案 %s：%s", path.name, e)
            raise HTTPException(422, "这份文案无法载入，请检查是否有可提词的内容")
        async with workspace_lock:
            await asyncio.gather(engine.reset(clear_viewport=True),
                                 inserts.reset(clear_comments=True, clear_viewport=True))
            script.use(selected)
            script_path, started = path, time.time()
            script_rev += 1
            rotate_recording()
            event = {"type": "script_selected", **script_info()}
            await bus.publish(event, clear_cards=True)
            await engine._status("idle" if engine.enabled else "disabled")
            await inserts.status()
            return {"file": script_path.name}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            async with workspace_lock:
                await inserts.expire_pending()
                # 先发送版本和当前房间，再补发快照；切换期间的新页面不会拿到混合数据。
                async with bus.send_lock:
                    await sock.send_json({
                        "type": "hello", **script_info(), "service_started": service_started,
                        "source": source_health(), "insert_proposal": inserts.pending})
                    for m in bus.snapshot():
                        if m["type"] != "source":
                            await sock.send_json(m)
                    bus.clients.add(sock)
            while True:
                try:
                    msg = json.loads(await sock.receive_text())
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                async with workspace_lock:
                    # 换稿／换房前发出的指令可能晚到，不能改到新的会话。
                    if msg.get("script_rev") != script_rev or msg.get("room_rev") != room_rev:
                        continue
                    t = msg.get("type")
                    if t == "viewport":
                        engine.set_viewport(msg.get("visible") or [], msg.get("reading"))
                        inserts.reading = engine.reading
                        if rec and engine.reading:
                            rec.reading(engine.reading)
                    elif t == "insert_now":
                        await inserts.trigger_now()
                    elif t == "insert_accept":
                        await inserts.expire_pending()
                        proposal_id = msg.get("id")
                        seg, before = inserts.accept(proposal_id)
                        if seg:
                            if rec:
                                rec.log("insert", segment=seg, before=before)
                            await bus.publish({"type": "script_insert", "segment": seg, "before": before,
                                               "proposal_id": proposal_id, "script": script.to_client()})
                            await inserts.status()
                    elif t == "insert_dismiss":
                        await inserts.expire_pending()
                        proposal_id = msg.get("id")
                        if inserts.dismiss(proposal_id):
                            if rec:
                                rec.log("insert_dismiss", id=proposal_id)
                            await bus.publish({"type": "insert_dismissed", "id": proposal_id})
                            await inserts.status()
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
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--live-id", help="直播间网址 live.douyin.com/<这串数字>，只连自己的直播间")
    g.add_argument("--mock", nargs="?", const="examples/mock_comments.txt",
                   help="模拟评论，可指定样例文件（默认 examples/mock_comments.txt）")
    g.add_argument("--manual", action="store_true", help="启动时不采集，可手动粘贴评论或从页面连接房间")
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
