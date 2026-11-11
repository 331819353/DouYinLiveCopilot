"""加载直播文案，提供按段落 id 查找、下一段摘要、直播中插入段等。"""
import json
from pathlib import Path


class Script:
    def __init__(self, path: Path):
        self.data = json.loads(Path(path).read_text("utf-8"))
        self.bible = self.data.get("bible", "")
        self._index()

    def _index(self):
        self.paragraphs = {}
        self.seg_of = {}
        for i, seg in enumerate(self.data["segments"]):
            for p in seg["paragraphs"]:
                self.paragraphs[p["id"]] = p
                self.seg_of[p["id"]] = i

    def segment_at(self, paragraph_id) -> dict | None:
        i = self.seg_of.get(paragraph_id)
        return self.data["segments"][i] if i is not None else None

    def next_segment(self, paragraph_id) -> dict | None:
        i = self.seg_of.get(paragraph_id)
        segs = self.data["segments"]
        return segs[i + 1] if i is not None and i + 1 < len(segs) else None

    def next_summary(self, paragraph_id: str | None) -> str:
        if paragraph_id not in self.seg_of:
            return ""
        nxt = self.next_segment(paragraph_id)
        return f"{nxt['title']}：{nxt.get('summary', '')}" if nxt else "这是最后一段。"

    def insert_after_current(self, segment: dict, reading_id) -> str | None:
        """把插入段放在主播当前所在段的后面（即下一段之前），不改动正在读的内容。
        返回它前面那一段之后的段 id（插在最后时为 None）。"""
        segs = self.data["segments"]
        i = self.seg_of.get(reading_id)
        pos = len(segs) if i is None else i + 1
        segs.insert(pos, segment)
        self._index()
        return segs[pos + 1]["id"] if pos + 1 < len(segs) else None

    def to_client(self) -> dict:
        return self.data
