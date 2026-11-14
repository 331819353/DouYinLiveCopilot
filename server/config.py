"""路径、环境变量和模型配置。"""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PROMPTS = ROOT / "prompts"
CONFIG = ROOT / "config"
LOGS = ROOT / "data" / "logs"
MODEL_SETTINGS = ROOT / "data" / "model-settings.json"
COMPATIBLE_PROVIDER = "openai-compatible"
COMPATIBLE_ID_PREFIX = "compatible-"

load_dotenv(ROOT / ".env", override=False)  # 系统环境里已有的同名变量优先

# 求助类卡片固定显示的资源，不交给模型生成。
# 12356：国家卫生健康委设立的全国统一心理援助热线，2025-05-01 起全国开通。
HELP_RESOURCES = "心理援助热线 12356；遇到紧急危险请拨打 110 或 120"


def base_llm_config() -> dict:
    return yaml.safe_load((CONFIG / "llm.yaml").read_text("utf-8"))


def local_model_settings() -> dict:
    try:
        return json.loads(MODEL_SETTINGS.read_text("utf-8"))
    except FileNotFoundError:
        return {}


def save_model_settings(settings: dict):
    MODEL_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件默认仅本用户可读写。写完再原子替换，避免半写入的密钥与配置。
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=MODEL_SETTINGS.parent,
                            prefix=".model-settings-", delete=False) as handle:
        path = Path(handle.name)
        try:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            path.replace(MODEL_SETTINGS)
        finally:
            path.unlink(missing_ok=True)


def load_llm_config(settings: dict | None = None) -> dict:
    cfg = base_llm_config()
    local = local_model_settings() if settings is None else settings
    cfg["routes"].update(local.get("routes", {}))
    # 内置供应商下新增的模型继续沿用预设端点和共用凭据。
    added = {
        name: custom_model(cfg["providers"][entry["base"]], entry["model"])
        for name, entry in local.get("added", {}).items()
        if name not in cfg["providers"] and entry["base"] in cfg["providers"]
    }
    cfg["providers"].update(added)
    # 兼容模型以地址和模型 ID 为标识。这里只合并公开配置，密钥仍通过 model_key 单独读取。
    cfg["providers"].update({
        name: compatible_model_config(name, entry["base_url"], entry["model"])
        for name, entry in local.get("compatible", {}).items()
    })
    for provider, model in local.get("models", {}).items():
        if provider not in cfg["providers"]:
            continue
        entry = cfg["providers"][provider]
        if model != entry["model"]:
            cfg["providers"][provider] = custom_model(entry, model)
    return cfg


def custom_model(entry: dict, model: str) -> dict:
    # 模型专用的推理参数和价格不能跟随新 ID，保留同服务的端点和基本请求格式。
    next_entry = {k: v for k, v in entry.items() if k not in {"label", "price"}}
    next_entry["model"] = model
    next_entry["extra_body"] = {k: v for k, v in entry.get("extra_body", {}).items() if k == "store"}
    return next_entry


def compatible_model_config(key_name: str, base_url: str, model: str) -> dict:
    # 兼容服务未必支持 response_format 参数，实际请求靠提示词要求 JSON，并在本地解析。
    return {"model": model, "base_url": base_url, "api_key_env": key_name, "compatible": True, "json_mode": False}


def model_key(env_name: str, settings: dict | None = None) -> str:
    local = local_model_settings() if settings is None else settings
    # 兼容模型的密钥只保存在该模型的本机配置中，与内置供应商的环境变量隔离。
    fallback = "" if env_name.startswith(COMPATIBLE_ID_PREFIX) else os.environ.get(env_name, "")
    return local.get("keys", {}).get(env_name) or fallback


def route(name: str, provider: str | None = None) -> tuple[str, dict]:
    """按任务默认路由取配置，写稿任务也可指定一个已配置的服务。"""
    cfg = load_llm_config()
    provider = provider or cfg["routes"][name]
    return provider, cfg["providers"][provider]


def filter_words() -> list[str]:
    p = CONFIG / "filter_words.txt"
    if not p.exists():
        return []
    return [w.strip() for w in p.read_text("utf-8").splitlines()
            if w.strip() and not w.lstrip().startswith("#")]
