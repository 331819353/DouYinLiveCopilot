# DouYinLiveCopilot

自用的抖音直播提词与评论互动助手：

- **左侧大屏：** 滚动显示 AI 生成的直播文案。
- **右上：** 实时评论。
- **右下：** AI 根据评论和当前文案给出的回复建议，由主播决定是否采用。

完整设计见 [DESIGN.md](DESIGN.md)。

> 本项目仅供个人学习研究使用。评论采集依赖第三方的非官方实现，可能随抖音改版失效，使用时需遵守平台规则和相关法律法规。

## 当前进度

| 阶段 | 状态 |
|---|---|
| M0 可行性 | 评论采集已在真实直播间跑通，两小时稳定性测试进行中；实时回复模型选定 `deepseek-flash`（推理 low） |
| M1 界面 | 已完成：三栏布局、滚动提词、悬停暂停、可视段落追踪、评论区、回复卡片 |
| M3 回复引擎 | 基本可用：过滤、触发、调用、校验；评测集和真实评论验证未完成 |
| M2 文案生成 | 未开始，目前使用 `examples/script_demo.json` 演示文案 |

## 目录

```
server/app.py                         本机服务入口：页面 + WebSocket
server/sources/webfetcher_worker.py   评论采集子进程，输出 JSON Lines 事件
server/sources/supervisor.py          管理采集子进程：重连、健康判断
server/reply/engine.py                实时回复引擎
server/reply/filters.py               评论预过滤（广告、低俗、重复、无信息）
static/                               前端页面（原生 JS，无需构建）
config/llm.yaml                       模型配置：各环节用哪个模型、地址、参数、价格
config/filter_words.txt               评论过滤词，可自行增删
prompts/reply_system.md               实时回复的系统提示词（当前为演示人设）
examples/                             演示文案和模拟评论
DESIGN.md                             设计方案
```

## 启动

```bash
.venv/bin/python -m server.app --mock                  # 模拟评论，先看效果
.venv/bin/python -m server.app --live-id 你的房间号     # 连接自己的直播间
.venv/bin/python -m server.app --manual                # 不采集，只手动粘贴评论
```

然后用浏览器打开 <http://127.0.0.1:8765>，全屏使用。服务只监听本机。

- **常用参数：**
  - `--script 文件路径`：指定直播文案，默认用演示文案。
  - `--no-reply`：关闭 AI 回复。
  - `--port`：换端口。
- **AI 回复需要先在 `.env` 里填 `DEEPSEEK_API_KEY`。** 没填时界面照常可用，只是右下角不出卡片。
- **模拟模式也会真实调用模型：** 每分钟约十几次，每次约 0.002 元，不用时请停掉服务。

## 操作

| 操作 | 效果 |
|---|---|
| 鼠标移到文案上 | 缓停；移开 0.3 秒后继续滚动 |
| 暂停时滚轮 | 前后翻看文案 |
| 空格 | 锁定暂停，再按一次继续 |
| `+` / `-` | 每次调整 10 字/分，范围 120～400 |
| 卡片“展开” | 显示完整回复，同时暂停文案；“收起”或“已用”后继续 |
| 评论上的“生成回复” | 立即为这条评论生成卡片 |
| 评论区底部输入框 | 手动输入或粘贴评论 |

右下角卡片的规则：
- 每张卡片都引用了当前屏幕上的一句文案原文，引用对不上的卡片会被程序丢弃。
- 求助类评论（如轻生倾向、未成年人要打赏、医疗求助）只显示给主播的提醒，以及固定的求助资源（心理援助热线 12356，紧急情况拨打 110 或 120）。

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

## 单独运行评论采集

一般不需要单独运行，`server.app` 会自动启动它。调试时可以这样用：

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
