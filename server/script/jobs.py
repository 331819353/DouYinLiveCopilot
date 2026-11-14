"""工作台逐章写稿。每次生成一章，等主播反馈后再推进。"""
import asyncio
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

from ..config import ROOT, base_llm_config, model_key, route
from ..llm import JsonLLM, LLMError
from ..profile import load_profile
from .pipeline import assemble, make_outline, produce_segment, propose_topics, save

TONES = {"natural": "自然聊天", "story": "故事叙述", "practical": "干货讲解"}
CHECKPOINT = ROOT / "data" / "script-workbench.json"
logger = logging.getLogger(__name__)


class ScriptOptions(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    topic_mode: Literal["custom", "auto"] = "custom"
    topic: str = Field(default="", max_length=100)
    persona: str = Field(default="", max_length=500)
    stance: str = Field(default="", max_length=500)
    audience: str = Field(default="", max_length=200)
    goal: str = Field(default="", max_length=240)
    avoid: str = Field(default="", max_length=300)
    materials: str = Field(default="", max_length=2600)
    use_profile_materials: bool = False
    minutes: int = Field(default=15, ge=5, le=120)
    cpm: int = Field(default=240, ge=120, le=400)
    tone: Literal["natural", "story", "practical"] = "natural"
    notes: str = Field(default="", max_length=500)
    provider: str = Field(default="", max_length=100)


class ChapterAction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    job_id: str
    version: int
    action: Literal["rewrite", "next", "finish", "retry", "end"]
    feedback: str = Field(default="", max_length=1000)
    carry: bool = False


def episode_profile(options: ScriptOptions) -> dict:
    profile, demo = load_profile()
    # 示例档案含虚构故事。本场任务只使用用户提供的真实素材和明确选用的实际档案素材。
    profile = {} if demo else profile
    materials = list(profile.get("materials", [])) if options.use_profile_materials else []
    mids = {m["id"] for m in materials}
    number = 1
    for line in options.materials.splitlines():
        if not line.strip():
            continue
        while f"episode{number}" in mids:
            number += 1
        materials.append({"id": f"episode{number}", "title": f"本场素材 {number}", "content": line.strip()})
        number += 1
    avoid = list(profile.get("avoid", []))
    if options.avoid:
        avoid.append(options.avoid)
    return profile | {"avoid": avoid, "materials": materials}


class ScriptJobs:
    def __init__(self, checkpoint: Path | None = None):
        self.checkpoint = checkpoint or CHECKPOINT
        self.job = None
        self.task: asyncio.Task | None = None
        try:
            self.job = json.loads(self.checkpoint.read_text("utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.exception("读取写稿进度失败")
        if self.job and "model_config" not in self.job and self.job["state"] in {"running", "review", "failed"}:
            # 老任务还没保存模型快照，从已记录的模型 ID 恢复，避免更改默认路由后接错模型。
            base = base_llm_config()
            saved_model = self.job.get("model")
            provider = self.job.get("provider") or next(
                (name for name, info in base["providers"].items() if saved_model and info["model"] == saved_model),
                base["routes"]["script"],
            )
            _, cfg = route("script", provider)
            old_cfg = base["providers"].get(provider, cfg)
            if saved_model and old_cfg["model"] == saved_model:
                cfg = old_cfg
            self.job.update(provider=provider, model_config=cfg)
        if self.job and self.job["state"] == "running":
            # 进程重启后，模型请求已经断开；只重试中断的操作，保留已交付的章节。
            self.job["state"] = "failed"
            self._log("服务重启中断了本次操作，可以从这里重试")

    def _checkpoint(self, job):
        # 先原子写入磁盘，再交换内存状态。写稿失败或服务重启时仍可恢复上一次成功结果。
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.checkpoint.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.checkpoint)
        self.job = job

    def snapshot(self):
        job = self.job
        if not job:
            return {"state": "idle"}
        outline = job.get("outline")
        result = {k: job[k] for k in ("id", "version", "state", "options", "logs", "operation_at",
                                      "carry_feedback")}
        result["title"] = (job.get("topic") or {}).get("title", "")
        result["chapters"] = []
        if outline:
            doc = self._document(job)
            result["title"] = doc["title"]
            for i, plan in enumerate(outline["segments"]):
                result["chapters"].append(doc["segments"][i] if i < len(doc["segments"]) else {
                    "id": f"s{i + 1}", "title": plan["title"], "summary": plan.get("summary", ""),
                    "planned_sec": plan["planned_sec"],
                })
        result["ready"] = len(job["results"])
        result["file"] = job.get("file")
        return result

    def _document(self, job):
        options = job["options"]
        elapsed = job["active_seconds"]
        meta = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "minutes": options["minutes"], "cpm": options["cpm"],
                "model": job["model"], "seconds": round(elapsed)}
        return assemble(job["outline"], job["results"], job["topic"], meta)

    def _log(self, message):
        now = time.time()
        self.job["logs"] = [*self.job["logs"], {"at": now, "message": message}][-200:]
        print(f"[文案 {self.job['id'][:8]}] {time.strftime('%H:%M:%S')} {message}", flush=True)

    def start(self, options: ScriptOptions):
        if self.job and self.job["state"] not in {"done", "ended"}:
            raise HTTPException(409, "已有写稿任务，请继续处理或结束该任务")
        if options.topic_mode == "custom" and not options.topic:
            raise HTTPException(422, "请输入直播主题，或选择自动选题")
        try:
            provider, config = route("script", options.provider)
        except KeyError as e:
            raise HTTPException(422, "找不到所选文案模型，请检查模型配置") from e
        if not config.get("model"):
            raise HTTPException(422, "所选文案模型还没有配置模型 ID")
        if not model_key(config["api_key_env"]):
            raise HTTPException(422, f"请在「模型设置」中填写 {config['api_key_env']}")
        job = {"id": uuid4().hex, "version": 0, "options": options.model_dump(), "topic": {"title": options.topic},
               "profile": episode_profile(options), "logs": [], "results": [],
               "chapter_feedback": [], "carry_feedback": [],
               "active_seconds": 0, "model": config["model"], "provider": provider, "model_config": config}
        return self._launch(job, {"kind": "prepare", "chapter": 1}, "已提交，准备大纲与第 1 章")

    def action(self, command: ChapterAction):
        job = self.job
        if not job or command.job_id != job["id"] or command.version != job["version"]:
            raise HTTPException(409, "任务已更新，请查看最新进度后再操作")
        if job["state"] == "running":
            raise HTTPException(409, "这一章正在处理")
        if command.action == "end" and job["state"] in {"review", "failed"}:
            self._log("已结束写稿")
            self._checkpoint(job | {"state": "ended", "version": job["version"] + 1})
            return self.snapshot()
        if command.action == "retry" and job["state"] == "failed":
            return self._launch(job, job["pending"], "重试上一步")
        if job["state"] != "review":
            raise HTTPException(409, "当前没有待确认的章节")

        chapter = len(job["results"])
        total = len(job["outline"]["segments"])
        if command.action == "rewrite":
            if not command.feedback:
                raise HTTPException(422, "请填写本章修改意见")
            return self._launch(job, {"kind": "rewrite", "chapter": chapter,
                                     "feedback": command.feedback, "carry": command.carry},
                                f"收到第 {chapter} 章的修改意见")
        if command.action == "next" and chapter < total:
            return self._launch(job, {"kind": "next", "chapter": chapter + 1, "feedback": command.feedback},
                                f"确认第 {chapter} 章，准备第 {chapter + 1} 章")
        if command.action == "finish" and chapter == total:
            return self._launch(job, {"kind": "finish", "chapter": chapter}, "整理完整草稿")
        raise HTTPException(409, "当前章节不支持此操作")

    def _launch(self, job, pending, message):
        job = job | {"state": "running", "pending": pending,
                     "version": job["version"] + 1, "operation_at": time.time()}
        self._checkpoint(job)
        self._log(message)
        self.task = asyncio.create_task(self._run(pending))
        return self.snapshot()

    async def _prepare(self, job, llm):
        options = job["options"]
        profile = job["profile"]
        # 本场人设是说话角色和语气参考，亲历素材仍从独立字段读取。
        brief = {"本场人设": options["persona"], "表达风格": TONES[options["tone"]], "观点偏向": options["stance"],
                 "目标观众": options["audience"], "直播目标": options["goal"],
                 "补充要求": options["notes"]}
        if options["topic_mode"] == "auto":
            self._log("根据人设和创作要求选题")
            topics = await propose_topics(llm, profile, [], minutes=options["minutes"], brief=brief)
            if not topics or not topics[0].get("title"):
                raise LLMError("没有生成可用的选题，可以重试或结束任务后改填主题")
            topic = topics[0]
        else:
            topic = {"title": options["topic"], "hook": "", "angle": "", "materials": []}
        topic = topic | {"brief": brief}
        self._log("生成大纲")
        outline = await make_outline(llm, profile, topic, options["minutes"], self._log)
        self._log(f"大纲完成，共 {len(outline['segments'])} 章")
        self._checkpoint(self.job | {"topic": topic, "outline": outline, "model": llm.cfg["model"]})

    async def _run(self, pending):
        try:
            if pending["kind"] == "finish":
                doc = self._document(self.job)
                # 审定页用本任务的素材核对亲历内容，旧文案继续使用默认主播档案。
                profile = self.job["profile"]
                doc["review_profile"] = {k: profile.get(k, default)
                                         for k, default in (("identity", ""), ("materials", []))}
                path = save(doc)
                self._log("完整草稿已保存，可进入审定或载入演示")
                self._checkpoint(self.job | {"state": "done", "file": path.name})
                return

            # 固定任务的模型与请求参数，密钥仍实时读取，方便补填或轮换。
            llm = JsonLLM("script", selection=(self.job["provider"], self.job["model_config"]))
            try:
                if not self.job.get("outline"):
                    await self._prepare(self.job, llm)
                updates = await self._write_chapter(llm, pending)
            finally:
                await llm.client.close()
            chapter = pending["chapter"]
            result = updates["results"][chapter - 1]
            issues = len(result["open_issues"])
            self._log(f"第 {chapter} 章已交付，待你确认" + (f"（{issues} 条审校提示）" if issues else ""))
            seconds = self.job["active_seconds"] + time.time() - self.job["operation_at"]
            self._checkpoint(self.job | updates | {"state": "review", "active_seconds": seconds})
        except APITimeoutError:
            self._fail("文案模型请求超时，可以重试上一步")
        except APIConnectionError:
            self._fail("无法连接文案模型，请检查网络后重试")
        except APIStatusError as e:
            self._fail(f"文案模型接口返回错误（{e.status_code}），请检查配置后重试")
        except LLMError as e:
            self._fail(str(e))
        except Exception:
            logger.exception("文案生成失败")
            self._fail("生成或保存失败，可以重试上一步")

    async def _write_chapter(self, llm, pending):
        job = self.job
        i = pending["chapter"] - 1
        rewrite = pending["kind"] == "rewrite"
        feedback = pending.get("feedback")
        carry = list(job["carry_feedback"])
        current = list(job["chapter_feedback"]) if rewrite else []
        if feedback:
            if rewrite:
                current.append(feedback)
            if not rewrite or pending["carry"]:
                carry.append(feedback)
        # 让下一章承接实际写过的内容。每章只取有限摘录，长直播也不会无限累积模型输入。
        previous = []
        for k, (plan, result) in enumerate(zip(job["outline"]["segments"][:i], job["results"][:i])):
            paras = result["paragraphs"]
            body = "\n".join(p["text"] for p in paras)
            previous.append({"title": plan["title"], "excerpt": body[:2400 if k == i - 1 else 420],
                             "ending": paras[-1]["text"][-250:]})
        # 本章多轮修改按提交顺序生效。已沿用的意见不在当前章的列表里重复出现。
        ordered = list(dict.fromkeys(reversed([*carry, *current])))
        outline = job["outline"] | {"brief": job["topic"]["brief"], "previous_chapters": previous,
                                    "chapter_feedback": list(reversed(ordered))}
        result = await produce_segment(llm, job["profile"], outline, i, job["options"]["cpm"],
                                       self._log, original=job["results"][i] if rewrite else None)
        if not any(p["text"].strip() for p in result["paragraphs"]):
            raise LLMError(f"第 {i + 1} 章没有返回口播内容，可以重试")
        # 重写或下一章生成失败时，保留上一版正文和意见；只在成功时提交新版本。
        results = job["results"][:i] + [result]
        return {"results": results, "carry_feedback": carry, "chapter_feedback": current}

    def _fail(self, note):
        self._log(note)
        failed = self.job | {"state": "failed"}
        try:
            self._checkpoint(failed)
        except OSError:
            # 磁盘暂时不可用也要解除页面的等待，保留内存结果供重试。
            logger.exception("无法保存写稿失败状态")
            self.job = failed

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
