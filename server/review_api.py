"""文案审定接口：列出、读取、保存、审定 data/scripts/ 下的文案。

保存时只接受对已有段落的修改（文字、类型、真实性、素材、删除）和顶层设定，
不整份覆盖；每次保存都重新跑程序检查，并清除之前的审定状态。
"""
import json
import re
import time

from fastapi import APIRouter, Body, HTTPException

from .config import ROOT
from .profile import load_profile
from .script.pipeline import SPEAK_RATIO, TRUTH, TYPES, check_outline_numbers, check_segment

SCRIPTS = ROOT / "data" / "scripts"
NAME = re.compile(r"[\w一-鿿.\-]+\.json")
TOP_FIELDS = ("title", "bible", "big_hook", "payoff")
router = APIRouter(prefix="/api/scripts")


def _path(name: str):
    if not NAME.fullmatch(name):
        raise HTTPException(400, "文件名不合法")
    p = (SCRIPTS / name).resolve()
    if p.parent != SCRIPTS.resolve() or not p.is_file():
        raise HTTPException(404, "找不到这份文案")
    return p


def _load(p):
    return json.loads(p.read_text("utf-8"))


def _save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _issue_count(d):
    return len(d.get("outline_issues", [])) + sum(
        len(s.get("review", {}).get("open_issues", [])) for s in d.get("segments", []))


def recheck(data: dict, profile: dict) -> dict:
    mids = {m["id"] for m in profile.get("materials", [])}
    cpm = (data.get("generated") or {}).get("cpm", 240)
    for seg in data["segments"]:
        review = seg.setdefault("review", {})
        target = review.get("target_chars") or int(seg["planned_sec"] / 60 * cpm * SPEAK_RATIO)
        review["target_chars"] = target
        review["open_issues"] = check_segment(seg["paragraphs"], mids, target)
        review["chars"] = sum(len(p.get("text", "")) for p in seg["paragraphs"])
    data["outline_issues"] = check_outline_numbers(data, profile)
    return data


@router.get("")
async def list_scripts():
    out = []
    for p in sorted(SCRIPTS.glob("*.json"), reverse=True):
        try:
            d = _load(p)
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"name": p.name, "title": d.get("title", ""),
                    "created_at": (d.get("generated") or {}).get("created_at", ""),
                    "approved_at": d.get("approved_at"), "issues": _issue_count(d),
                    "segments": len(d.get("segments", [])),
                    "chars": sum(len(q.get("text", "")) for s in d.get("segments", [])
                                 for q in s.get("paragraphs", []))})
    return out


@router.get("/{name}")
async def get_script(name: str):
    profile, demo = load_profile()
    return {"script": _load(_path(name)), "materials": profile.get("materials", []),
            "demo_profile": demo}


@router.put("/{name}")
async def save_script(name: str, body: dict = Body(...)):
    p = _path(name)
    data = _load(p)
    for k in TOP_FIELDS:
        if isinstance(body.get(k), str):
            data[k] = body[k].strip()[:2000]
    edits = {s.get("id"): s for s in body.get("segments", []) if isinstance(s, dict)}
    for seg in data["segments"]:
        e = edits.get(seg["id"])
        if not e:
            continue
        by_id = {q.get("id"): q for q in e.get("paragraphs", []) if isinstance(q, dict)}
        kept = []
        for para in seg["paragraphs"]:
            q = by_id.get(para["id"])
            if q is None:
                kept.append(para)
                continue
            if q.get("deleted"):
                continue
            if isinstance(q.get("text"), str):
                para["text"] = q["text"].strip()[:400]
            if q.get("type") in TYPES:
                para["type"] = q["type"]
            if q.get("truth_level") in TRUTH:
                para["truth_level"] = q["truth_level"]
            mid = q.get("material_id")
            if isinstance(mid, str):
                if mid:
                    para["material_id"] = mid[:20]
                else:
                    para.pop("material_id", None)
            kept.append(para)
        seg["paragraphs"] = kept
    data.pop("approved_at", None)  # 改过就需要重新审定
    data.pop("approved_open_issues", None)
    profile, _ = load_profile()
    _save(p, recheck(data, profile))
    return {"script": data}


@router.post("/{name}/approve")
async def approve_script(name: str):
    p = _path(name)
    profile, _ = load_profile()
    data = recheck(_load(p), profile)
    data["approved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["approved_open_issues"] = _issue_count(data)
    _save(p, data)
    return {"script": data}
