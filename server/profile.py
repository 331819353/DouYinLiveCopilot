"""主播档案：文案生成和实时回复共用。"""
import yaml

from .config import CONFIG


def load_profile() -> tuple[dict, bool]:
    """返回 (档案, 是否为演示档案)。没有 config/profile.yaml 时退回到演示档案。"""
    own = CONFIG / "profile.yaml"
    if own.exists():
        return yaml.safe_load(own.read_text("utf-8")), False
    return yaml.safe_load((CONFIG / "profile.example.yaml").read_text("utf-8")), True


def persona_text(p: dict, with_materials: bool = False) -> str:
    lines = [f"- 称呼：{p.get('name', '主播')}",
             f"- 身份：{p.get('identity', '')}",
             f"- 关注领域：{'、'.join(p.get('domains', []))}",
             f"- 价值观：{'；'.join(p.get('values', []))}",
             f"- 绝不碰：{'；'.join(p.get('avoid', []))}",
             f"- 风格：{p.get('style', '')}"]
    if with_materials:
        lines.append("- 真实素材：")
        lines += [f"  - [{m['id']}] {m['title']}：{m['content']}" for m in p.get("materials", [])]
    return "\n".join(lines)
