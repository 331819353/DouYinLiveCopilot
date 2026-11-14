"""直播文案生成流水线：选题 → 大纲 → 分段写作 → 审校 → 计时。

流程由代码按阶段编排，每一步是一次模型调用，便于定位问题和重试。
硬性规则（真实性标注、虚构提示、字数）由程序检查；事实与红线由模型审校。
"""
import asyncio
import json
import re
import time
from pathlib import Path

from ..config import PROMPTS, ROOT
from ..llm import JsonLLM
from ..profile import persona_text

OUT_DIR = ROOT / "data" / "scripts"
SPEAK_RATIO = 0.65  # 文案口播时间占直播时长的比例，其余留给互动（估算，彩排后校准）
TYPES = {"hook", "story", "method", "interaction", "recap"}
TRUTH = {"lived", "fictional", "opinion"}
SIGNPOST = re.compile(r"编的|虚构|假设|打个比方|举个例子|设想|想象一下|比如说有|不是真事|我编")
ASK = re.compile(r"扣[个一\d“\"「]|扣\s*\d|打在公屏|评论区(打|说|告诉)|打个数字")
RECAP = re.compile(r"刚进来的|新来的朋友|刚来的朋友|补个课")


def rules_text() -> str:
    return (PROMPTS / "script_rules.md").read_text("utf-8")


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------- 1. 选题 ----------------

async def propose_topics(llm: JsonLLM, profile: dict, hot: list[dict], n: int = 5,
                         minutes: int = 120, brief: dict | None = None) -> list[dict]:
    hot_txt = "\n".join(
        f"- [{h['source']}] {h['title']}" + (f"（热度 {h['popularity']}）" if h.get("popularity") else "")
        for h in hot[:80]) or "（本次没有热榜，请从主播的关注领域出发，选常青话题）"
    user = f"""# 主播档案
{persona_text(profile, with_materials=True)}

# 今日热榜（只有标题，细节未核实）
{hot_txt}

# 创作偏好（主题与表达参考，服从真实性和内容规则）
{json.dumps(brief or {}, ensure_ascii=False)}

# 任务
提出 {n} 个适合这位主播讲 {minutes} 分钟的直播选题。
- 选题必须落在主播的关注领域里，能用上真实素材；热榜里和领域相关的话题可以作为引子，不相关的不要硬蹭。
- 有本场人设、观点、听众或目标时，按本场创作偏好选题；没有可用的真实素材时，就选适合讲观点或方法的主题。
- 标题要有悬念、让人想留下来，但不能夸大，不能暗示丑闻或两性关系。
- 标题必须如实：只用素材原意，不能把两件素材拼在一起制造“以为是 A，其实是 B”的假反转。
- 话题要能撑起多个故事，能持续引发讨论。

只输出 json：
{{"topics": [{{"title": "直播标题", "hook": "开场第一句", "angle": "主线怎么展开，50 字内",
  "related_hot": ["关联的热榜标题，没有就留空数组"], "materials": ["可用的素材 id"],
  "scores": {{"relevance": 1-5, "story": 1-5, "discussion": 1-5, "risk": 1-5}},
  "risk_note": "主要风险点"}}]}}
其中 risk 越高越危险。"""
    data = await llm.json(rules_text(), user)
    topics = data.get("topics", [])
    for t in topics:
        s = t.get("scores", {})
        t["rank"] = _int(s.get("relevance")) + _int(s.get("story")) + _int(s.get("discussion")) - _int(s.get("risk"))
    return sorted(topics, key=lambda t: -t["rank"])


# ---------------- 2. 大纲 ----------------

async def make_outline(llm: JsonLLM, profile: dict, topic: dict, minutes: int, log=None) -> dict:
    nseg = max(3, round(minutes / 15))
    user = f"""# 主播档案
{persona_text(profile, with_materials=True)}

# 选定的选题
{json.dumps(topic, ensure_ascii=False)}

# 任务
把这个选题拆成 {nseg} 段，直播总时长 {minutes} 分钟，各段 planned_sec 加起来等于 {minutes * 60}。
- 第一段抛出全场大悬念，最后一段兑现；中间每段都要有自己的小悬念和故事。
- 结合选题里的创作偏好设计主线、论点及互动方式。本场人设仅用于角色表达，亲历只能依据真实素材。
- 尽量把真实素材分配到不同段落；没有素材支撑的地方用观点或明确标注的虚构故事。
- 每段写清楚目标、3～6 条要点，以及引出下一段的一句话。

只输出 json：
{{"title": "直播标题", "bible": "全场设定，100～200 字：主题、主线、大悬念及其答案、已用的梗",
  "big_hook": "全场大悬念", "payoff": "大悬念的答案",
  "segments": [{{"title": "段标题", "planned_sec": 900, "goal": "这一段要达到的效果",
    "summary": "一句话概括", "beats": ["要点"], "materials": ["素材 id"], "teaser": "引出下一段的一句话"}}]}}"""
    outline = await llm.json(rules_text(), user)
    # 节目设定会作为背景交给实时回复，里面的事实也必须出自素材
    if log:
        log("审校大纲")
    model_issues = await review_outline(llm, profile, outline)
    issues = check_outline_numbers(outline, profile) + model_issues
    if issues:
        if log:
            log(f"大纲发现 {len(issues)} 条审校提示，正在调整")
        fix = (f"{user}\n\n# 上一版大纲\n{json.dumps(outline, ensure_ascii=False)}\n\n# 审校发现的问题\n"
               + "\n".join(f"- {s}" for s in issues) + "\n\n请修改后重新输出完整大纲，格式不变。")
        outline = await llm.json(rules_text(), fix)
        # 重写后重新对照素材，原稿的问题是否已解决要由审校确认。
        if log:
            log("复审调整后的大纲")
        model_issues = await review_outline(llm, profile, outline)
    outline["outline_model_issues"] = model_issues
    outline["outline_issues"] = check_outline_numbers(outline, profile) + model_issues
    segs = outline.get("segments") or []
    if not segs:
        raise ValueError("大纲里没有段落")
    total = sum(max(60, _int(s.get("planned_sec"), 600)) for s in segs)
    for s in segs:  # 按比例校正，保证总时长准确
        s["planned_sec"] = round(max(60, _int(s.get("planned_sec"), 600)) * minutes * 60 / total)
    return outline


NUM = re.compile(r"\d+(?:\.\d+)?")
CN_AMOUNT = re.compile(r"[一二两三四五六七八九十几]+[千万]")


def check_outline_numbers(outline: dict, profile: dict) -> list[str]:
    """节目设定、大悬念和答案里的数字，必须能在素材原文里找到。"""
    source = " ".join(m["content"] for m in profile.get("materials", [])) + " " + profile.get("identity", "")
    issues = []
    for field in ("bible", "big_hook", "payoff"):
        text = outline.get(field, "")
        for n in sorted(set(NUM.findall(text))):
            if n not in source:
                issues.append(f"{field} 里的数字“{n}”在素材里找不到，请删掉或改成素材里的说法")
        for n in sorted(set(CN_AMOUNT.findall(text))):
            if n not in source:
                issues.append(f"{field} 里的“{n}”在素材里找不到，请删掉或改成素材里的说法")
    return issues


async def review_outline(llm: JsonLLM, profile: dict, outline: dict) -> list[str]:
    brief = {k: outline.get(k) for k in ("title", "bible", "big_hook", "payoff")}
    user = f"""# 主播档案（含真实素材）
{persona_text(profile, with_materials=True)}

# 待审大纲
{json.dumps(brief, ensure_ascii=False)}

# 任务
对照真实素材检查标题、节目设定、大悬念和答案：
1. 是否出现素材里没有的事实、数字或情节，或改变了素材原意。
2. 是否把两件素材拼在一起，制造“以为是 A，其实是 B”的假反转。
3. 是否夸大，或触碰红线。
只报真正的问题。只输出 json：{{"issues": ["问题和修改建议"]}}，没有问题就输出空数组。"""
    data = await llm.json(rules_text(), user)
    return [str(x) for x in data.get("issues", []) if x]


# ---------------- 3. 分段写作 ----------------

def _segment_context(profile, outline, i):
    seg = outline["segments"][i]
    overview = "\n".join(f"{k + 1}. {s['title']}：{s.get('summary', '')}"
                         for k, s in enumerate(outline["segments"]))
    context = f"""# 主播档案（含真实素材）
{persona_text(profile, with_materials=True)}

# 节目圣经
{outline.get('bible', '')}
大悬念：{outline.get('big_hook', '')}（答案：{outline.get('payoff', '')}，只能在最后一段揭晓）

# 创作偏好（表达参考，服从真实性和内容规则）
{json.dumps(outline.get('brief', {}), ensure_ascii=False)}

# 全场大纲
{overview}

# 本段：第 {i + 1}/{len(outline['segments'])} 段
{json.dumps(seg, ensure_ascii=False)}"""
    # 逐章模式提供已确认的事实与衔接，命令行并行模式仍只使用大纲。
    if outline.get("previous_chapters"):
        context += ("\n\n# 已确认章节（只用于承接，避免重复讲述）\n"
                    + json.dumps(outline["previous_chapters"], ensure_ascii=False))
    if outline.get("chapter_feedback"):
        context += ("\n\n# 主播对本章的最新要求\n"
                    "要求按提交顺序排列。互相矛盾时，以最新要求为准；原大纲安排可调整，"
                    "仍须遵守真实性、内容规则，并和已确认章节衔接。"
                    "\n" + json.dumps(outline["chapter_feedback"], ensure_ascii=False))
    return context


PARA_SCHEMA = """只输出 json：
{"paragraphs": [{"type": "hook|story|method|interaction|recap", "truth_level": "lived|fictional|opinion",
  "material_id": "lived 时必填素材 id，其他留空", "text": "口播原文"}]}"""


async def write_segment(llm, profile, outline, i, cpm) -> tuple[list[dict], int]:
    seg = outline["segments"][i]
    target = int(seg["planned_sec"] / 60 * cpm * SPEAK_RATIO)
    user = f"""{_segment_context(profile, outline, i)}

# 任务
写这一段的口播文案，总字数约 {target} 字（允许上下浮动 15%）。
按要点和本章最新要求展开，结尾自然引出下一章（最后一章则兑现开场承诺并收尾）。

{PARA_SCHEMA}"""
    data = await llm.json(rules_text(), user)
    return data.get("paragraphs", []), target


def check_segment(paras: list[dict], material_ids: set, target: int) -> list[str]:
    """程序检查硬性规则，返回问题列表。"""
    issues, prev = [], None
    for k, p in enumerate(paras, 1):
        text, tl = p.get("text", ""), p.get("truth_level")
        if p.get("type") not in TYPES:
            issues.append(f"第 {k} 段 type 不合法：{p.get('type')}")
        if tl not in TRUTH:
            issues.append(f"第 {k} 段 truth_level 只能是 lived / fictional / opinion")
        if tl == "lived" and p.get("material_id") not in material_ids:
            issues.append(f"第 {k} 段标为亲历，但没有对应的真实素材，请改为观点或明确的虚构")
        if tl == "fictional" and prev != "fictional" and not SIGNPOST.search(text):
            issues.append(f"第 {k} 段开始讲虚构内容，但没有让观众听出是虚构（如“这个故事是我编的”“假设一下”）")
        if not 20 <= len(text) <= 160:
            issues.append(f"第 {k} 段 {len(text)} 字，应在 40～120 字左右")
        prev = tl
    total = sum(len(p.get("text", "")) for p in paras)
    if target and abs(total - target) / target > 0.3:
        issues.append(f"本段共 {total} 字，目标约 {target} 字，相差超过 30%")
    # 节奏：互动和回顾太密会显得机械
    asks = sum(1 for p in paras if ASK.search(p.get("text", "")))
    limit = max(3, round(len(paras) / 4))
    if asks > limit:
        issues.append(f"本段有 {asks} 个段落在让观众扣字或打字，最多 {limit} 个，请合并或删掉一些")
    recaps = sum(1 for p in paras if p.get("type") == "recap" or RECAP.search(p.get("text", "")))
    if recaps > 1:
        issues.append(f"本段有 {recaps} 处“刚进来的朋友”式回顾，只保留 1 处")
    return issues


# ---------------- 4. 审校与重写 ----------------

async def review_segment(llm, profile, outline, i, paras) -> list[str]:
    mats = {m["id"]: m["content"] for m in profile.get("materials", [])}
    numbered = "\n".join(
        f"{k}. [{p.get('type')}/{p.get('truth_level')}"
        f"{'/' + p['material_id'] if p.get('material_id') else ''}] {p.get('text', '')}"
        + (f"\n   对应素材原文：{mats[p['material_id']]}" if p.get("material_id") in mats else "")
        for k, p in enumerate(paras, 1))
    user = f"""{_segment_context(profile, outline, i)}

# 待审文案（lived 段落后附有对应素材原文）
{numbered}

# 任务
逐段检查，只报真正的问题，不要挑文风：
1. 标为 lived 的段落，逐句对照素材原文：是否添加了素材里没有的事实、数字、人物、情节或心理活动；是否改变了素材原意（例如把“没乱花”说成“没动”）。
2. 是否编造了热点事件的细节、数据或当事人言行。
3. 是否触碰红线，或档案里“绝不碰”的内容。
4. 钩子是否夸大，或者许诺了后面不会兑现的内容；是否把两件素材拼在一起制造假反转；大悬念是否提前泄露。
5. 是否与节目圣经或真实素材矛盾。

只输出 json：{{"issues": [{{"index": 段落序号, "problem": "问题", "fix": "怎么改"}}]}}，没有问题就输出空数组。"""
    data = await llm.json(rules_text(), user)
    return [f"第 {it.get('index')} 段：{it.get('problem')}（建议：{it.get('fix')}）"
            for it in data.get("issues", []) if it.get("problem")]


async def rewrite_segment(llm, profile, outline, i, paras, issues, target) -> list[dict]:
    user = f"""{_segment_context(profile, outline, i)}

# 原稿
{json.dumps({"paragraphs": paras}, ensure_ascii=False)}

# 审校发现的问题
{chr(10).join('- ' + s for s in issues)}

# 任务
根据本章最新要求和上述审校意见修改原稿，其余内容尽量保持不变，总字数约 {target} 字。

{PARA_SCHEMA}"""
    data = await llm.json(rules_text(), user)
    return data.get("paragraphs", [])


async def produce_segment(llm, profile, outline, i, cpm, log, original=None) -> dict:
    seg = outline["segments"][i]
    mids = {m["id"] for m in profile.get("materials", [])}
    chapter = f"第 {i + 1} 章"
    rewritten = original is not None
    if original is None:
        log(f"{chapter}「{seg['title']}」写作中")
        paras, target = await write_segment(llm, profile, outline, i, cpm)
    else:
        log(f"{chapter}按意见重写")
        target = original["target_chars"]
        paras = await rewrite_segment(llm, profile, outline, i, original["paragraphs"],
                                      ["执行主播对本章的最新要求"], target)
    log(f"{chapter}审校中")
    model_issues = await review_segment(llm, profile, outline, i, paras)
    issues = check_segment(paras, mids, target) + model_issues
    if issues:
        log(f"{chapter}发现 {len(issues)} 条审校提示，正在调整")
        paras = await rewrite_segment(llm, profile, outline, i, paras, issues, target)
        # 调整后仍须复审，主播意见也继续留在提示词里。
        log(f"{chapter}复审中")
        model_issues = await review_segment(llm, profile, outline, i, paras)
        issues = check_segment(paras, mids, target) + model_issues
        rewritten = True
    return {"paragraphs": paras, "target_chars": target, "rewritten": rewritten,
            "open_issues": issues, "model_issues": model_issues}


# ---------------- 5. 组装与计时 ----------------

def assemble(outline, results, topic, meta) -> dict:
    segments = []
    for i, (seg, res) in enumerate(zip(outline["segments"], results), 1):
        paras = []
        for k, p in enumerate(res["paragraphs"], 1):
            q = {"id": f"s{i}p{k}", "type": p.get("type") if p.get("type") in TYPES else "story",
                 "truth_level": p.get("truth_level") if p.get("truth_level") in TRUTH else "opinion",
                 "text": p.get("text", "").strip()}
            if p.get("material_id"):
                q["material_id"] = p["material_id"]
            paras.append(q)
        chars = sum(len(p["text"]) for p in paras)
        segments.append({"id": f"s{i}", "title": seg["title"], "planned_sec": seg["planned_sec"],
                         "summary": seg.get("summary", ""), "paragraphs": paras,
                         "review": {"chars": chars, "target_chars": res["target_chars"],
                                    "rewritten": res["rewritten"], "open_issues": res["open_issues"],
                                    "model_issues": res["model_issues"]}})
    return {"title": outline.get("title") or topic.get("title"), "bible": outline.get("bible", ""),
            "big_hook": outline.get("big_hook", ""), "payoff": outline.get("payoff", ""),
            "outline_issues": outline["outline_issues"], "outline_model_issues": outline["outline_model_issues"],
            "generated": meta | {"topic": topic}, "segments": segments}


def timing_report(script: dict, cpm: int) -> list[str]:
    lines, total_chars, total_plan = [], 0, 0
    for k, s in enumerate(script["segments"], 1):
        chars = s["review"]["chars"]
        total_chars += chars
        total_plan += s["planned_sec"]
        lines.append(f"  第 {k} 段 {s['title'][:16]:<16} {chars:>5} 字，纯念 {chars / cpm:>4.1f} 分钟"
                     f" / 计划 {s['planned_sec'] / 60:.0f} 分钟"
                     + (f"，待看 {len(s['review']['open_issues'])} 处" if s['review']['open_issues'] else ""))
    lines.append(f"  合计 {total_chars} 字，按 {cpm} 字/分纯念约 {total_chars / cpm:.0f} 分钟，"
                 f"计划 {total_plan / 60:.0f} 分钟（其余留给互动）")
    return lines


def save(script: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w一-鿿]+", "", script["title"])[:20] or "script"
    # 时间戳细化到纳秒，避免命令行和工作台同时生成同名文案时覆盖。
    path = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000000:09d}_{slug}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # 列表和审定页只读取已经完整写入的 JSON
    return path


async def generate(profile, topic, minutes, cpm, llm, log, parallel=4) -> dict:
    t0 = time.time()
    log("生成大纲")
    outline = await make_outline(llm, profile, topic, minutes, log)
    outline["brief"] = topic.get("brief", {})
    log(f"大纲完成：{len(outline['segments'])} 段 · {outline.get('title', '')}")
    sem = asyncio.Semaphore(parallel)

    async def write(i):
        async with sem:
            result = await produce_segment(llm, profile, outline, i, cpm, log)
            log(f"第 {i + 1} 章完成")
            return result

    tasks = [asyncio.create_task(write(i)) for i in range(len(outline["segments"]))]
    try:
        results = await asyncio.gather(*tasks)
    finally:
        # 某段失败或服务关闭时，收回其他分段调用，避免后台继续改写进度。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    meta = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "minutes": minutes, "cpm": cpm,
            "model": llm.cfg["model"], "seconds": round(time.time() - t0)}
    return assemble(outline, results, topic, meta)
