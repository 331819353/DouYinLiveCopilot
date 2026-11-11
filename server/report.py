"""下播复盘：把一场的评论、在线人数、AI 卡片和主播读到的位置按时间对齐到文案段落。

  .venv/bin/python -m server.report latest
  .venv/bin/python -m server.report data/sessions/20261111-213000

输出 Markdown 报告到该会话目录下的 report.md。
"""
import argparse
import json
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from .config import ROOT
from .session import SESSIONS


def fmt_min(sec):
    return f"{sec / 60:.1f}"


def load(session_dir: Path):
    meta = json.loads((session_dir / "meta.json").read_text("utf-8"))
    script = json.loads((session_dir / "script.json").read_text("utf-8"))
    events = []
    with open(session_dir / "events.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 进程被强行结束时，最后一行可能不完整
    # 直播中插入的段落：按记录放回文案快照里对应的位置
    for e in events:
        if e["type"] == "insert" and e.get("segment"):
            segs = script["segments"]
            idx = next((k for k, s in enumerate(segs) if s["id"] == e.get("before")), len(segs))
            segs.insert(idx, e["segment"])
    return meta, script, events


def build_report(meta, script, events) -> str:
    seg_of, seg_titles, planned = {}, [], []
    for i, seg in enumerate(script["segments"]):
        seg_titles.append(seg["title"].replace("|", "｜"))  # 避免撑破 Markdown 表格
        planned.append(seg.get("planned_sec", 0))
        for p in seg["paragraphs"]:
            seg_of[p["id"]] = i
    start = meta["started"]
    end = events[-1]["t"] if events else start

    # 阅读位置时间线：每个时刻属于哪一段
    marks = [(e["t"], seg_of[e["p"]]) for e in events if e["type"] == "reading" and e.get("p") in seg_of]
    times = [t for t, _ in marks]

    def seg_at(t):
        i = bisect_right(times, t) - 1
        return marks[i][1] if i >= 0 else None

    n = len(seg_titles)
    spent = [0.0] * n
    for k, (t, s) in enumerate(marks):
        spent[s] += (marks[k + 1][0] if k + 1 < len(marks) else end) - t

    comments = [e for e in events if e["type"] == "comment"]
    stats = [e for e in events if e["type"] == "stats" and e.get("online") is not None]
    cards = {e["id"]: e for e in events if e["type"] == "card"}
    actions = [e for e in events if e["type"] == "card_action" and e.get("id")]

    per = defaultdict(lambda: {"comments": 0, "questions": 0, "filtered": 0, "online": [],
                               "cards": 0, "used": 0, "dismissed": 0})
    for c in comments:
        s = seg_at(c["t"])
        per[s]["comments"] += 1
        per[s]["questions"] += bool(c.get("q"))
        per[s]["filtered"] += bool(c.get("filtered"))
    for st in stats:
        per[seg_at(st["t"])]["online"].append(st["online"])
    for c in cards.values():
        per[seg_at(c["t"])]["cards"] += 1
    final_action = {}
    for a in actions:
        final_action[a["id"]] = a["action"]
    for cid, act in final_action.items():
        if cid in cards:
            s = seg_at(cards[cid]["t"])
            per[s]["used"] += act == "used"
            per[s]["dismissed"] += act == "dismiss"

    viewers = {c.get("uid") for c in comments if c.get("uid") and c.get("source") != "manual"}
    online_all = [s["online"] for s in stats]
    lines = [f"# 下播复盘：{script.get('title', '')}", "",
             f"- 时长：{fmt_min(end - start)} 分钟（模式：{meta.get('mode')}）",
             f"- 评论：{len(comments)} 条，发言人数约 {len(viewers)}，"
             f"其中提问 {sum(bool(c.get('q')) for c in comments)} 条、被过滤 {sum(bool(c.get('filtered')) for c in comments)} 条",
             f"- 在线：最高 {max(online_all) if online_all else '-'}，"
             f"共 {len(online_all)} 次统计（小直播间推送很少，数据可能稀疏）",
             f"- AI 卡片：{len(cards)} 张，标记已用 {sum(a == 'used' for a in final_action.values())} 张，"
             f"忽略 {sum(a == 'dismiss' for a in final_action.values())} 张，"
             f"求助类 {sum(c.get('risk') == 'escalate' for c in cards.values())} 张",
             ""]
    if not marks:
        lines += ["> 没有记录到阅读位置（页面可能没打开），无法按段落对齐。", ""]

    lines += ["## 各段表现", "",
              "| 段落 | 实际/计划（分钟） | 评论 | 每分钟评论 | 提问 | 在线 首→尾 | 卡片 | 已用 |",
              "|---|---|---|---|---|---|---|---|"]
    for i in range(n):
        d = per[i]
        on = d["online"]
        rate = d["comments"] / (spent[i] / 60) if spent[i] > 30 else 0
        lines.append(f"| {i + 1}. {seg_titles[i]} | {fmt_min(spent[i])} / {fmt_min(planned[i])} | {d['comments']} "
                     f"| {rate:.1f} | {d['questions']} | {f'{on[0]}→{on[-1]}' if on else '-'} "
                     f"| {d['cards']} | {d['used']} |")
    if per[None]["comments"]:
        lines.append(f"| 页面打开前 | - | {per[None]['comments']} | - | {per[None]['questions']} | - | - | - |")
    lines.append("")

    # 结论：只列数据支持的事实，不做推测
    notes = []
    timed = [i for i in range(n) if spent[i] > 60]
    if timed:
        hot = max(timed, key=lambda i: per[i]["comments"] / spent[i])
        notes.append(f"评论最活跃的是第 {hot + 1} 段「{seg_titles[hot]}」，"
                     f"每分钟 {per[hot]['comments'] / (spent[hot] / 60):.1f} 条。")
        over = [i for i in timed if planned[i] and spent[i] > planned[i] * 1.3]
        if over:
            notes.append("比计划多用 30% 以上时间的段落：" + "、".join(f"第 {i + 1} 段" for i in over) + "。")
    drops = [(i, per[i]["online"][0] - per[i]["online"][-1]) for i in range(n) if len(per[i]["online"]) >= 2]
    if drops:
        i, d = max(drops, key=lambda x: x[1])
        if d > 0:
            notes.append(f"在线人数下降最多的是第 {i + 1} 段「{seg_titles[i]}」，减少 {d} 人。")
    if cards:
        used = sum(a == "used" for a in final_action.values())
        notes.append(f"AI 卡片采用率 {used / len(cards):.0%}（以“已用”按钮为准，口头采用但没点按钮的不计）。")
    if notes:
        lines += ["## 要点", ""] + [f"- {s}" for s in notes] + [""]

    qs = list(dict.fromkeys(c["text"] for c in comments if c.get("q") and not c.get("filtered")))
    if qs:
        lines += ["## 观众提问（前 15 条）", ""] + [f"- {q}" for q in qs[:15]] + [""]
    words = Counter(c["text"] for c in comments if not c.get("filtered"))
    repeated = [(t, k) for t, k in words.most_common(10) if k >= 3]
    if repeated:
        lines += ["## 重复出现的评论", ""] + [f"- {t}（{k} 次）" for t, k in repeated] + [""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="下播复盘")
    ap.add_argument("session", nargs="?", default="latest", help="会话目录，或 latest")
    args = ap.parse_args()
    if args.session == "latest":
        dirs = sorted(d for d in SESSIONS.glob("*") if (d / "events.jsonl").exists()) if SESSIONS.exists() else []
        if not dirs:
            sys.exit("还没有会话记录。开播时运行 server.app 会自动录制。")
        d = dirs[-1]
    else:
        d = (ROOT / args.session) if not Path(args.session).is_absolute() else Path(args.session)
    report = build_report(*load(d))
    out = d / "report.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n已保存：{out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()
