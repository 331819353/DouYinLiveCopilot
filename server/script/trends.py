"""热榜来源：通过 MCP 调用 mcp-trends-hub，或读取手工整理的热榜文件。

mcp-trends-hub 固定为 1.7.0（MIT）。2026-11-11 审查过它的发布包：只向各站公开热榜接口
发请求，缓存写在系统临时目录，不执行子进程、不读取其他环境变量、没有安装脚本。
升级版本前请重新审查。
"""
import asyncio
import os
import re
from pathlib import Path

TRENDS_PACKAGE = "mcp-trends-hub@1.7.0"
# 默认只用国内中文来源
SOURCES = {
    "weibo": ("get_weibo_trending", {}),
    "zhihu": ("get_zhihu_trending", {"limit": 30}),
    "douyin": ("get_douyin_trending", {}),
    "toutiao": ("get_toutiao_trending", {}),
    "thepaper": ("get_thepaper_trending", {}),
}
FIELD = re.compile(r"<(\w+)>(.*?)</\1>", re.S)


def _parse_item(text: str) -> dict:
    return {k: v.strip() for k, v in FIELD.findall(text)}


async def fetch_trends(sources=None, per_source=20, timeout=60) -> list[dict]:
    """返回 [{source, title, popularity, description, link}]。单个来源失败不影响其他来源。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ, TRENDS_HUB_HIDDEN_FIELDS="cover")  # 不要封面图链接
    params = StdioServerParameters(command="npx", args=["-y", TRENDS_PACKAGE], env=env)
    items, errors = [], []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), timeout)
            for src in sources or SOURCES:
                tool, args = SOURCES[src]
                try:
                    res = await asyncio.wait_for(s.call_tool(tool, args), timeout)
                except Exception as e:  # 单个来源超时或报错
                    errors.append(f"{src}: {e!r}"[:120])
                    continue
                if res.isError:
                    errors.append(f"{src}: {(res.content[0].text if res.content else '')[:120]}")
                    continue
                for c in res.content[:per_source]:
                    it = _parse_item(getattr(c, "text", ""))
                    if it.get("title"):
                        items.append({"source": src, "title": it["title"],
                                      "popularity": it.get("popularity", ""),
                                      "description": it.get("description", "")[:120],
                                      "link": it.get("link", "")})
    for e in errors:
        print(f"[热榜] 获取失败 {e}")
    return items


def load_hot_file(path) -> list[dict]:
    """手工热榜：每行一条，可写成“来源<TAB>标题”或只写标题；# 开头为注释。"""
    items = []
    for line in Path(path).read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        src, _, title = line.partition("\t") if "\t" in line else ("手工", "", line)
        items.append({"source": src.strip(), "title": title.strip(), "popularity": "",
                      "description": "", "link": ""})
    return items
