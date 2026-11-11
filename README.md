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
| M2 文案生成 | 基本可用：选题、大纲、分段写作、审校、计时、审定页；热榜接入的实际运行尚未验证 |
| 复盘 | 基本可用：开播自动录制，下播生成按段落对齐的复盘报告 |

## 目录

```
server/app.py                         本机服务入口：页面 + WebSocket
server/sources/webfetcher_worker.py   评论采集子进程，输出 JSON Lines 事件
server/sources/supervisor.py          管理采集子进程：重连、健康判断
server/reply/engine.py                实时回复引擎
server/reply/filters.py               评论预过滤（广告、低俗、重复、无信息）
server/script/                        文案生成：热榜、选题、大纲、分段写作、审校
server/review_api.py                  文案审定接口（页面在 static/review.*）
server/session.py                     开播录制
server/report.py                      下播复盘报告
server/profile.py                     主播档案读取
config/profile.example.yaml           主播档案模板（复制为 profile.yaml 后填写）
prompts/script_rules.md               文案通用规则：真实性、红线、钩子与节奏
static/                               前端页面（原生 JS，无需构建）
config/llm.yaml                       模型配置：各环节用哪个模型、地址、参数、价格
config/filter_words.txt               评论过滤词，可自行增删
prompts/reply_system.md               实时回复的系统提示词（当前为演示人设）
examples/                             演示文案和模拟评论
DESIGN.md                             设计方案
```

## 主播档案

文案生成和实时回复共用一份档案，内容包括身份、关注领域、价值观、绝不碰的内容、说话风格和真实素材。

```bash
cp config/profile.example.yaml config/profile.yaml   # 然后改成你自己的内容
```

`profile.yaml` 不进版本库。没有这个文件时会使用演示档案（虚构人设“小林”）。

**真实素材最重要。** 文案里标为“亲历”的故事只能来自素材，AI 可以润色，但不能添加事实。素材越多越具体，文案越真实，也越不容易被要求改成虚构。

## 生成文案

```bash
.venv/bin/python -m server.script                         # 拉热榜，列出 5 个候选选题后选择
.venv/bin/python -m server.script --hot-file hot.txt      # 用手工整理的热榜，每行一条
.venv/bin/python -m server.script --no-trends --pick 1    # 不用热榜，直接取排名第一的选题
.venv/bin/python -m server.script --topic "自己定的标题"   # 跳过选题，直接按这个题目写
```

**常用参数：**
- `--minutes`：直播时长，默认 120。
- `--cpm`：语速（字/分），默认 240。
- `--parallel`：同时写几段，默认 4。

**流程：** 选题 → 大纲 → 各段并行写作 → 审校 → 计时。
- **程序检查：** 亲历段落必须对应真实素材；虚构故事开头必须让观众听得出是虚构；字数是否接近目标。
- **模型审校：** 有没有添加素材外的事实、编造热点细节、触碰红线、夸大钩子。
- **重写：** 有问题的段落重写一次，还剩的问题记在文案文件的 `review.open_issues` 里，需要你人工看一下。

### 开播前审定

```bash
.venv/bin/python -m server.app --review    # 打开 http://127.0.0.1:8765/review
```

审定页可以做这些事：
- 逐段通读文案，修改文字，调整段落类型和真实性标注，选择对应素材，或者删除段落。
- 标为“亲历”的段落下方会直接显示素材原文，方便对照有没有添油加醋。
- 点“保存并重新检查”会重跑程序检查。
- 确认无误后点“审定通过”。审定后再改动，需要重新审定。

开播界面加载未审定的文案时，顶栏会显示“文案未审定”，点它直接跳到审定页。

生成的文案保存在 `data/scripts/`，用 `--script latest` 加载：

```bash
.venv/bin/python -m server.app --script latest --live-id 你的房间号
```

**热榜来源：** 热榜通过 MCP 调用 [mcp-trends-hub](https://github.com/baranwang/mcp-trends-hub)，固定为 1.7.0 版本，代码已审查过。它会通过 `npx` 下载并运行，需要 Node.js。默认只取微博、知乎、抖音、头条、澎湃的热榜；热榜只有标题，文案里只把热点当引子，不编造事件细节。

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

## 下播复盘

开播时会自动录制本场数据到 `data/sessions/<开始时间>/`，加 `--no-record` 可以关闭。
- **录制内容：** 评论、在线人数、你读到哪一段、AI 卡片，以及你对卡片的操作。
- **隐私：** 不记录昵称，用户 ID 只保存哈希。
- **保存期限：** 只存本机，7 天后自动删除。

下播后生成复盘报告：

```bash
.venv/bin/python -m server.report latest
```

**报告内容：**
- 各段实际用时与计划用时对比。
- 各段的评论数、每分钟评论、提问数、在线人数首尾变化。
- AI 卡片数量和采用率，以“已用”按钮为准。
- 观众提问列表和重复出现的评论。

报告保存为该会话目录下的 `report.md`。只有开播页面开着时，才能记录读到的位置，并按段落对齐。

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
