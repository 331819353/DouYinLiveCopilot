# DouYinLiveCopilot

自用的抖音直播提词与评论互动助手：

- **左侧大屏：** 滚动显示 AI 生成的直播文案。
- **右上：** 实时评论。
- **右下：** AI 根据评论和当前文案给出的回复建议，由主播决定是否采用。

完整设计见 [DESIGN.md](DESIGN.md)。

> 本项目仅供个人学习研究使用。评论采集依赖第三方的非官方实现，可能随抖音改版失效，使用时需遵守平台规则和相关法律法规。

## 当前进度

**M0 可行性验证：**
- 评论采集已在真实直播间跑通，长时间稳定性测试仍在进行。
- 实时回复模型已选定 `deepseek-flash`，推理强度 low。

界面（M1）、文案生成（M2）、回复引擎（M3）尚未开始。

## 目录

```
config/llm.yaml                       模型配置：各环节用哪个模型、地址、参数、价格
prompts/reply_system.md               实时回复的系统提示词（当前为演示人设）
server/sources/webfetcher_worker.py   评论采集子进程，输出 JSON Lines 事件
DESIGN.md                             设计方案
```

## 准备环境

需要 [uv](https://docs.astral.sh/uv/) 和 Node.js（采集库计算签名时要用）。

```bash
uv sync --python 3.12
cp .env.example .env    # 然后在 .env 里填 DEEPSEEK_API_KEY
```

### 获取评论采集库

评论采集基于 [saermart/DouyinLiveWebFetcher](https://github.com/saermart/DouyinLiveWebFetcher)（AGPL-3.0）。它不包含在本仓库里，需要单独拉取到 `vendor/` 下。本项目适配的是 `40a904b` 这个提交：

```bash
git clone https://github.com/saermart/DouyinLiveWebFetcher.git vendor/DouyinLiveWebFetcher
git -C vendor/DouyinLiveWebFetcher checkout 40a904b
```

该库作者声明仅限学习研究，禁止商业用途。

## 评论采集

```bash
.venv/bin/python server/sources/webfetcher_worker.py --live-id 你的房间号
.venv/bin/python server/sources/webfetcher_worker.py --mock-seconds 10   # 离线模拟
```

房间号就是直播间网址 `live.douyin.com/` 后面那串数字。**只连接自己的直播间。**

**stdout 事件**（每行一个 JSON）：

| `type` | 含义 |
|---|---|
| `starting` / `open` / `closed` | 连接状态 |
| `chat` | 评论。用户 ID 只保留加盐哈希，同时记录 `msg_id` 等时间字段 |
| `stats` | 在线人数。小直播间里很少推送 |
| `gift` / `like` | 礼物、点赞 |
| `alive` | 每 30 秒一次，报告收到的帧数和消息类型，用于判断连接是否还活着 |
| `parse_error` / `ws_error` / `fatal` | 错误 |
| `ended` | 直播已结束 |

**退出码：** 0 = 直播结束；1 = 连接断开，可重连；2 = 启动失败（如取不到 room_id）。

采集库自身的输出全部改到 stderr，不会混进事件流。

## 许可证

[AGPL-3.0](LICENSE)，与所依赖的评论采集库保持一致。
