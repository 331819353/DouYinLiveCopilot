"""加载直播文案，提供按段落 id 查找、下一段摘要等。"""
import json
from pathlib import Path


class Script:
    def __init__(self, path: Path):
        self.data = json.loads(Path(path).read_text("utf-8"))
        self.bible = self.data.get("bible", "")
        self.paragraphs = {}
        self.seg_of = {}
        for i, seg in enumerate(self.data["segments"]):
            for p in seg["paragraphs"]:
                self.paragraphs[p["id"]] = p
                self.seg_of[p["id"]] = i

    def next_summary(self, paragraph_id: str | None) -> str:
        if paragraph_id not in self.seg_of:
            return ""
        i = self.seg_of[paragraph_id] + 1
        segs = self.data["segments"]
        if i >= len(segs):
            return "这是最后一段。"
        return f"{segs[i]['title']}：{segs[i].get('summary', '')}"

    def to_client(self) -> dict:
        return self.data
