"""生成直播文案。

  .venv/bin/python -m server.script                         # 拉热榜，列出候选选题后选择
  .venv/bin/python -m server.script --hot-file hot.txt      # 用手工整理的热榜
  .venv/bin/python -m server.script --no-trends --pick 1    # 不用热榜，直接取排名第一的选题
  .venv/bin/python -m server.script --topic "自己定的标题"   # 跳过选题，直接按这个题目写

生成的文案保存在 data/scripts/，用 `python -m server.app --script latest` 加载最新一份。
"""
import argparse
import asyncio
import sys

from ..llm import JsonLLM, LLMError
from ..profile import load_profile
from .pipeline import generate, propose_topics, save, timing_report
from .trends import fetch_trends, load_hot_file


def log(msg):
    print(f"· {msg}", flush=True)


def choose(topics, pick):
    for k, t in enumerate(topics, 1):
        s = t.get("scores", {})
        print(f"\n[{k}] {t.get('title')}\n    开场：{t.get('hook')}\n    主线：{t.get('angle')}"
              f"\n    评分：相关 {s.get('relevance')} · 故事 {s.get('story')} · 讨论 {s.get('discussion')}"
              f" · 风险 {s.get('risk')}（{t.get('risk_note', '')}）"
              + (f"\n    关联热榜：{'、'.join(t['related_hot'])}" if t.get("related_hot") else ""))
    if pick:
        return topics[pick - 1]
    if not sys.stdin.isatty():
        print("\n非交互模式，取排名第一的选题。")
        return topics[0]
    while True:
        ans = input(f"\n选择一个选题（1-{len(topics)}），直接回车选 1：").strip() or "1"
        if ans.isdigit() and 1 <= int(ans) <= len(topics):
            return topics[int(ans) - 1]


async def main_async(args):
    profile, demo = load_profile()
    if demo:
        print("提示：没有找到 config/profile.yaml，正在使用演示档案。"
              "请复制 config/profile.example.yaml 为 profile.yaml 并改成你自己的内容。\n")
    llm = JsonLLM("script")
    print(f"文案模型：{llm.provider}（{llm.cfg['model']}）")

    if args.topic:
        topic = {"title": args.topic, "hook": "", "angle": "", "materials": []}
    else:
        hot = []
        if args.hot_file:
            hot = load_hot_file(args.hot_file)
        elif not args.no_trends:
            log("获取热榜")
            try:
                hot = await fetch_trends()
            except Exception as e:  # 热榜不可用时退回到常青话题
                print(f"[热榜] 获取失败：{e!r}；改为不使用热榜")
        log(f"热榜 {len(hot)} 条，生成候选选题")
        topics = await propose_topics(llm, profile, hot)
        if not topics:
            raise LLMError("模型没有给出选题")
        topic = choose(topics, args.pick)
    print(f"\n选定：{topic.get('title')}\n")

    script = await generate(profile, topic, args.minutes, args.cpm, llm, log, args.parallel)
    path = save(script)
    print("\n===== 完成 =====")
    print("\n".join(timing_report(script, args.cpm)))
    u = llm.usage
    print(f"  模型调用 {u['calls']} 次，用时 {script['generated']['seconds']} 秒，"
          f"约 ¥{llm.cost_yuan():.2f}（按空闲时段价格）")
    print(f"\n已保存：{path}\n加载：.venv/bin/python -m server.app --script latest --mock")


def main():
    ap = argparse.ArgumentParser(description="生成直播文案")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--hot-file", help="手工热榜文件：每行一条，可写“来源<TAB>标题”")
    src.add_argument("--no-trends", action="store_true", help="不使用热榜")
    src.add_argument("--topic", help="直接指定题目，跳过选题")
    ap.add_argument("--pick", type=int, help="直接选第几个候选选题")
    ap.add_argument("--minutes", type=int, default=120, help="直播时长（分钟），默认 120")
    ap.add_argument("--cpm", type=int, default=240, help="语速（字/分钟），默认 240")
    ap.add_argument("--parallel", type=int, default=4, help="同时写几段，默认 4")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except LLMError as e:
        sys.exit(f"失败：{e}")
    except KeyboardInterrupt:
        sys.exit("已取消")


if __name__ == "__main__":
    main()
