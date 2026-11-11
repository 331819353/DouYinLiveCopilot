"""路径、环境变量和模型配置。"""
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
PROMPTS = ROOT / "prompts"
CONFIG = ROOT / "config"
LOGS = ROOT / "data" / "logs"

load_dotenv(ROOT / ".env", override=False)  # 系统环境里已有的同名变量优先

# 求助类卡片固定显示的资源，不交给模型生成。
# 12356：国家卫生健康委设立的全国统一心理援助热线，2025-05-01 起全国开通。
HELP_RESOURCES = "心理援助热线 12356；遇到紧急危险请拨打 110 或 120"


def load_llm_config() -> dict:
    return yaml.safe_load((CONFIG / "llm.yaml").read_text("utf-8"))


def route(name: str) -> tuple[str, dict]:
    cfg = load_llm_config()
    provider = cfg["routes"][name]
    return provider, cfg["providers"][provider]


def filter_words() -> list[str]:
    p = CONFIG / "filter_words.txt"
    if not p.exists():
        return []
    return [w.strip() for w in p.read_text("utf-8").splitlines()
            if w.strip() and not w.lstrip().startswith("#")]
