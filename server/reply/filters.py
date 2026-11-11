"""评论预过滤：在送给模型之前，用规则拦掉广告、低俗、重复和无信息量的评论。"""
import re
import time
from collections import deque

AD = re.compile(r"加\s*[vV微]|[vV][xX]|威信|私信|看主页|主页有|日结|返利|扫码|代理|代购|链接")
NO_INFO = re.compile(r"^[哈呵嘿啊哦嗯噢6\s!！。.~～]+$")
NUMBER = re.compile(r"\s*\d+(\.\d+)?\s*[元块kK]?\s*")
STRIP = re.compile(r"[\s\W_]+", re.U)
QUESTION = re.compile(r"[?？]|吗|怎么|为什么|为啥|多少|如何|能不能|是不是|哪")


class CommentFilter:
    def __init__(self, words: list[str]):
        self.words = words
        self.recent: deque = deque(maxlen=300)  # (时间, uid, 内容)，用于识别重复刷屏

    def check(self, uid: str, text: str) -> str | None:
        """返回过滤原因；None 表示通过。"""
        now = time.time()
        core = STRIP.sub("", text)
        if NUMBER.fullmatch(text):
            reason = "数字"  # 多半是在回应“扣 1 / 打个数字”，界面照常显示，但不送给 AI
        elif len(core) < 2:
            reason = "过短"
        elif NO_INFO.match(text):
            reason = "无信息"
        elif AD.search(text):
            reason = "广告"
        elif any(w in text for w in self.words):
            reason = "过滤词"
        elif any(u == uid and c == text and now - t < 60 for t, u, c in self.recent):
            reason = "重复"
        else:
            reason = None
        self.recent.append((now, uid, text))
        return reason


def is_question(text: str) -> bool:
    return bool(QUESTION.search(text))
