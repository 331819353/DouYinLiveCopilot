import { initModelSettings } from "./model-settings.js";
import { askWorkspace } from "./workspace-dialog.js";

// 页面操作走 HTTP，直播内容与当前房间使用 WebSocket 广播，以服务端状态为准。
const $ = (id) => document.getElementById(id);

async function api(path, method = "GET", data) {
  const r = await fetch(path, {
    method, cache: "no-store",
    ...(data === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }),
  });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error(typeof body?.detail === "string" ? body.detail : "请求失败，请检查填写项后重试");
  return body;
}

function note(id, value, error = false) {
  const node = $(id);
  node.dataset.error = error;
  if (node.textContent !== value) node.textContent = value;
}

export function reviewUrl(file) {
  return file ? `/review?file=${encodeURIComponent(file)}` : "/review";
}

const config = $("script-config");
const form = $("generate-form");
const fields = $("generate-fields");
const submit = $("generate-submit");
const scriptModel = form.elements.provider;
const library = $("saved-script");
const result = $("job-actions");
const chapterForm = $("chapter-form");
const feedback = $("chapter-feedback");
const carry = $("chapter-carry");
const chapterSelect = $("chapter-select");
const jobPanel = $("job-panel");
const workspace = $("script-pane");
const tabs = { config: $("config-toggle"), chapters: $("job-toggle"), read: $("read-toggle") };
const GENERATION = "/api/workbench/generation";
let state = null;
let poll;
let requestInProgress = false;
let generationRequest = 0;
let formRestored = false;
let savedProvider = "";
let models;
let scriptLoading = false;
let selectedChapter = 0;
let chapterKey = "";
let logsKey = "";
const viewKey = "copilot-workspace-view";
let lastView;
try { lastView = sessionStorage.getItem(viewKey); } catch { /* 浏览器可能限制会话存储 */ }

function setView(view) {
  if (tabs[view].disabled || workspace.dataset.view === view) return;
  workspace.dataset.view = view;
  config.hidden = view !== "config";
  jobPanel.hidden = view !== "chapters";
  $("prompter").toggleAttribute("data-away", view !== "read");
  $("prompter").inert = view !== "read";
  for (const [name, tab] of Object.entries(tabs)) tab.setAttribute("aria-pressed", String(name === view));
  // 配置与章节视图都暂停提词，返回时滚动器按同一个段落恢复。
  document.dispatchEvent(new CustomEvent("workbench-view", { detail: view }));
  // 记录当前标签页选中的视图，刷新后可以继续在原处查看草稿或提词。
  lastView = view;
  try { sessionStorage.setItem(viewKey, view); } catch { /* 存储不可用时仍可切换 */ }
}

function topicMode() {
  const custom = form.elements.topic_mode.value === "custom";
  form.elements.topic.required = custom;
  form.elements.topic.closest("label").hidden = !custom;
}

function make(tag, value, className) {
  const node = document.createElement(tag);
  if (value != null) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function showLogs(logs) {
  const last = logs.at(-1)?.at;
  const key = JSON.stringify([state.id, logs.length, last]);
  if (logsKey === key) return;
  logsKey = key;
  const list = $("job-logs");
  const stick = list.scrollHeight - list.scrollTop - list.clientHeight < 32;
  const previousTop = list.scrollTop;
  list.replaceChildren(...logs.map(({ at, message }) => {
    const item = make("li");
    const time = make("time", new Date(at * 1000).toLocaleTimeString("zh-CN", { hour12: false }));
    time.dateTime = new Date(at * 1000).toISOString();
    item.append(time, document.createTextNode(message));
    return item;
  }));
  list.scrollTop = stick ? list.scrollHeight : previousTop;
}

function showElapsed() {
  const elapsed = $("job-elapsed");
  if (state?.state !== "running") {
    elapsed.textContent = "";
    return;
  }
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - state.operation_at));
  elapsed.textContent = "本步已用 " + (seconds < 60 ? seconds + " 秒"
    : Math.floor(seconds / 60) + " 分 " + (seconds % 60) + " 秒");
}

function showChapter() {
  const { chapters, ready, version, state: status } = state;
  const inReview = status === "review";
  const lastChapter = ready === chapters.length;
  $("job-title").textContent = state.title || "逐章写稿";
  $("job-count").textContent = chapters.length ? `已生成 ${ready} / ${chapters.length} 章` : "正在准备章节";
  $("chapter-picker").hidden = chapters.length === 0;
  const key = JSON.stringify([state.id, version, status, ready, selectedChapter, chapters.length]);
  // 轮询时保留阅读位置、已打开的审校提示和正在输入的意见。
  if (chapterKey !== key) {
    chapterKey = key;
    $("carry-summary").hidden = state.carry_feedback.length === 0;
    $("carry-list").replaceChildren(...state.carry_feedback.map((item) => make("li", item)));
    chapterSelect.replaceChildren(...chapters.map((chapter, i) => new Option(
      `第 ${i + 1} 章 · ${chapter.title}${i < ready ? "" : "（大纲）"}`, String(i))));
    chapterSelect.value = String(selectedChapter);
    const pane = $("chapter-preview");
    pane.replaceChildren();
    const chapter = chapters[selectedChapter];
    if (chapter) {
      pane.append(make("h3", chapter.title));
      if (chapter.paragraphs) {
        const copy = make("div", null, "chapter-copy");
        for (const para of chapter.paragraphs) {
          const line = make("p");
          if (para.truth_level === "fictional") line.append(make("span", "虚构 · 需说明", "tag fictional"));
          line.append(document.createTextNode(para.text));
          copy.append(line);
        }
        pane.append(copy);
        const issues = chapter.review?.open_issues || [];
        if (issues.length) {
          const details = make("details", null, "chapter-review");
          const list = make("ul");
          list.append(...issues.map((issue) => make("li", issue)));
          details.append(make("summary", `${issues.length} 条审校提示`), list);
          pane.append(details);
        }
      } else {
        pane.append(make("p", chapter.summary || "这一章在大纲中，正文待生成。", "form-note"));
      }
    } else {
      const text = status === "running" ? "第一章完成后会在这里显示。"
        : status === "ended" ? "本次写稿已结束。" : "可以从生成日志查看详情。";
      pane.append(make("p", text, "form-note"));
    }
  }
  chapterForm.hidden = !inReview || selectedChapter !== ready - 1;
  $("chapter-next").hidden = lastChapter;
  $("chapter-finish").hidden = !lastChapter;
  $("chapter-carry-wrap").hidden = lastChapter;
  $("feedback-hint").textContent = lastChapter ? "要修改这一章，请填写意见后重写，再确认保存。"
    : "重写会修改本章。点「确认本章并继续」时，意见只用于后续章节。";
  $("job-retry").hidden = status !== "failed";
  $("job-end").hidden = !inReview && status !== "failed";
  const buttons = [...chapterForm.querySelectorAll("button"), $("job-retry"), $("job-end")];
  for (const button of buttons) button.disabled = requestInProgress || status === "running";
  $("chapter-rewrite").disabled ||= !feedback.value.trim();
}

function showGeneration(job) {
  const previous = state;
  state = job;
  if (!formRestored && job.options) {
    // 刷新后恢复任务配置，后续轮询不覆盖用户修改的表单。
    for (const [name, value] of Object.entries(job.options)) {
      const field = form.elements.namedItem(name);
      if (!field) continue;
      if (field === scriptModel) {
        savedProvider = value;
        restoreProvider();
      } else if (field.type === "checkbox") field.checked = value;
      else field.value = value;
    }
    topicMode();
  }
  formRestored = true;
  fields.disabled = requestInProgress || ["running", "review", "failed"].includes(job.state);
  submit.textContent = fields.disabled ? "已有写稿任务" : "开始写第 1 章";
  tabs.chapters.disabled = job.state === "idle";
  result.hidden = job.state !== "done";
  if (job.state === "idle") {
    if (!previous || workspace.dataset.view === "chapters") {
      setView(lastView === "read" ? "read" : "config");
    }
    if (previous) $("job-note").hidden = true;
    return;
  }
  if (!previous) setView(Object.hasOwn(tabs, lastView) ? lastView : "chapters");
  $("job-note").hidden = false;
  if (previous?.id !== job.id || previous.ready !== job.ready) {
    selectedChapter = Math.max(0, job.ready - 1);
    jobPanel.querySelector(".chapter-document").scrollTop = 0;
  }
  const last = job.logs.at(-1)?.message || "";
  if (job.state === "done") {
    $("job-review").href = reviewUrl(job.file);
    note("job-note", `草稿已保存 · ${job.title}`);
    if (previous?.state !== "done") loadLibrary();
  } else {
    note("job-note", last, job.state === "failed");
  }
  $("job-note").title = $("job-note").textContent;
  showChapter();
  showLogs(job.logs);
  showElapsed();
}

function schedulePoll() {
  clearTimeout(poll);
  poll = setTimeout(readGeneration, state?.state === "running" ? 1200 : 4500);
}

async function readGeneration() {
  const request = ++generationRequest;
  try {
    const job = await api(GENERATION);
    if (request === generationRequest) showGeneration(job);
  } catch {
    if (request !== generationRequest) return;
    $("job-note").hidden = false;
    note("job-note", "暂时无法读取写稿进度，正在重试…", true);
  } finally {
    if (request === generationRequest) schedulePoll();
  }
}

async function loadLibrary() {
  try {
    const scripts = await api("/api/scripts");
    const selected = library.value;
    library.replaceChildren(new Option("启动时使用的文案", "@initial"),
      ...scripts.map((s) => new Option(`${s.title || s.name} · ${s.approved_at ? "已审定" : "未审定"}`, s.name)));
    if ([...library.options].some((opt) => opt.value === selected)) library.value = selected;
    $("library-note").hidden = true;
  } catch {
    $("library-note").hidden = false;
    note("library-note", "无法读取文案库，重新打开配置可重试", true);
  }
}

async function loadScript(name, button) {
  if (scriptLoading) return;
  scriptLoading = button.disabled = true;
  try {
    await api("/api/workbench/script", "PUT", { name });
    setView("read");
    if (button.id === "job-load") tabs.read.focus();
    if (state?.state !== "running") {
      $("job-note").hidden = false;
      note("job-note", "已载入文案，从头开始演示。");
    }
  } catch (e) {
    if (button.id === "job-load") {
      note("job-note", e.message, true);
    } else {
      $("library-note").hidden = false;
      note("library-note", e.message, true);
    }
  } finally {
    scriptLoading = button.disabled = false;
  }
}

for (const [view, tab] of Object.entries(tabs)) {
  tab.addEventListener("click", () => setView(view));
}
form.elements.topic_mode.addEventListener("change", topicMode);
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (fields.disabled || submit.disabled || requestInProgress) return;
  const options = Object.fromEntries(new FormData(form));
  options.minutes = Number(options.minutes);
  options.cpm = Number(options.cpm);
  options.use_profile_materials = form.elements.use_profile_materials.checked;
  if (options.topic_mode === "auto") options.topic = "";
  requestInProgress = fields.disabled = true;
  clearTimeout(poll);
  generationRequest++;
  try {
    const job = await api(GENERATION, "POST", options);
    requestInProgress = false;
    feedback.value = "";
    carry.checked = false;
    $("chapter-error").hidden = true;
    showGeneration(job);
    setView("chapters");
    tabs.chapters.focus();
  } catch (e) {
    requestInProgress = false;
    fields.disabled = false;
    $("job-note").hidden = false;
    note("job-note", e.message, true);
  } finally {
    schedulePoll();
  }
});

function showModelNote() {
  const id = scriptModel.value || models?.default;
  const model = models?.providers.find((item) => item.id === id);
  const missing = model && !model.ready;
  note("model-note", missing ? "请在顶部「模型设置」填写此模型的 API 密钥。" : "", Boolean(missing));
  submit.disabled = Boolean(missing);
}

function restoreProvider() {
  // 模型列表和已保存任务分别加载，任一先返回都能恢复上次的选择。
  if (models) {
    scriptModel.value = models.providers.some((model) => model.id === savedProvider && model.model) ? savedProvider : "";
    showModelNote();
  }
}

function useModels(data) {
  models = data;
  const fallback = models.providers.find((item) => item.id === models.default);
  scriptModel.replaceChildren(new Option(fallback ? `默认 · ${fallback.label}` : "使用默认模型", ""),
    ...models.providers.filter((item) => item.model).map((item) => new Option(item.label, item.id)));
  restoreProvider();
}

async function loadModels() {
  try {
    useModels(await api("/api/workbench/models"));
  } catch {
    note("model-note", "暂时无法读取模型选项，可刷新重试。", true);
  }
}

scriptModel.addEventListener("change", () => {
  savedProvider = scriptModel.value;
  showModelNote();
});

initModelSettings({ api, note, make, onUpdate: useModels });

async function sendChapter(action) {
  if (requestInProgress || !state?.id) return;
  if (action === "finish" && feedback.value.trim()) {
    $("chapter-error").hidden = false;
    note("chapter-error", "还有未提交的意见，请先重写本章，或清空意见后保存。", true);
    feedback.focus();
    return;
  }
  requestInProgress = true;
  clearTimeout(poll);
  generationRequest++;
  $("chapter-error").hidden = true;
  showChapter();
  try {
    const job = await api(GENERATION + "/action", "POST", {
      job_id: state.id, version: state.version, action,
      feedback: ["rewrite", "next"].includes(action) ? feedback.value.trim() : "",
      carry: action === "rewrite" && carry.checked,
    });
    requestInProgress = false;
    if (!["end", "retry"].includes(action)) {
      feedback.value = "";
      carry.checked = false;
    }
    showGeneration(job);
    tabs.chapters.focus();
  } catch (e) {
    requestInProgress = false;
    $("chapter-error").hidden = false;
    note("chapter-error", e.message, true);
    showChapter();
  } finally {
    schedulePoll();
  }
}

chapterSelect.addEventListener("change", () => {
  selectedChapter = Number(chapterSelect.value);
  showChapter();
  jobPanel.querySelector(".chapter-document").scrollTop = 0;
});
feedback.addEventListener("input", () => {
  $("chapter-error").hidden = true;
  $("chapter-rewrite").disabled = requestInProgress || !feedback.value.trim();
});
chapterForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.submitter) sendChapter(e.submitter.value);
});
$("job-retry").addEventListener("click", () => sendChapter("retry"));
$("job-end").addEventListener("click", async () => {
  const job = state?.id;
  const confirmed = await askWorkspace({
    title: "结束写稿",
    message: "结束后仍可查看本次已生成的章节。开始新任务时，本次进度会被替换。",
    action: "结束写稿",
  });
  // 确认期间工作台仍在轮询，只结束打开弹窗时对应的任务。
  if (confirmed && state?.id === job) sendChapter("end");
});
setInterval(showElapsed, 1000);

$("job-load").addEventListener("click", (e) => loadScript(state.file, e.currentTarget));
$("library-form").addEventListener("submit", (e) => {
  e.preventDefault();
  loadScript(library.value, e.submitter || e.currentTarget.querySelector("button"));
});

let roomState;
let roomAction = null;
let roomError = "";
let serviceReady = false;
const roomInput = $("room-input");
const roomConnect = $("room-connect");
const roomStop = $("room-stop");
const activeRoomLabels = { starting: "连接中…", ok: "已连接", quiet: "已连接", reconnecting: "自动重连中…" };

function parseRoom(value) {
  // 与服务端接受的格式一致。粘贴同一房间的链接也应视为已选中该房间。
  const match = /^(?:([0-9]{1,30})|https?:\/\/live\.douyin\.com\/([0-9]{1,30})\/?(?:[?#][^\s]*)?)$/.exec(value);
  return match && (match[1] || match[2]);
}

function renderRoom() {
  const room = roomState?.room;
  const status = roomState?.state;
  const value = roomInput.value.trim();
  const target = parseRoom(value);
  const sameRoom = Boolean(room && target === room);
  const activeLabel = sameRoom ? activeRoomLabels[status] : "";
  const mock = roomState?.kind === "mock";
  $("room-current").textContent = room ? `房间 ${room}`
    : mock ? "模拟评论" : "未连接房间";
  roomInput.disabled = Boolean(roomAction);
  roomConnect.disabled = Boolean(roomAction) || !serviceReady || !target || Boolean(activeLabel);
  roomConnect.dataset.connected = String(serviceReady && !roomAction && activeLabel === "已连接");
  roomStop.disabled = Boolean(roomAction) || !serviceReady;
  roomStop.hidden = !room && !mock;
  roomStop.textContent = mock ? "停止模拟" : "断开";

  let label = room ? (sameRoom ? "重新连接" : "换房") : "连接";
  if (activeLabel) label = activeLabel;
  if (!serviceReady) label = "服务重连中…";
  else if (roomAction) label = roomAction === "stop" ? "断开中…" : "连接中…";
  roomConnect.textContent = label;

  const invalid = Boolean(value && !target);
  roomInput.setAttribute("aria-invalid", String(invalid));
  let message = room ? roomState.note : mock ? "正在接收模拟评论。" : "输入房间号或抖音直播间链接。";
  if (invalid) message = "请输入数字房间号，或完整的 live.douyin.com 直播间链接。";
  if (roomError) message = roomError;
  if (roomAction) message = roomAction === "stop" ? "正在断开评论来源…" : "正在更新评论来源…";
  if (!serviceReady) message = "正在连接本机服务…";
  const failed = ["dead", "ended", "stopped"].includes(status);
  note("room-note", message, serviceReady && !roomAction && Boolean(roomError || invalid || failed));
}

export function showRoom(source) {
  // 健康状态定时刷新，只在房间变更时同步输入框，避免覆盖正在输入的房间号。
  if (roomState?.room_rev !== source.room_rev || roomState?.room !== source.room) {
    roomInput.value = source.room || "";
    roomError = "";
  }
  roomState = source;
  renderRoom();
}

const manualInput = $("manual-input");
const manualSend = $("manual-send");

export function syncManualSend() {
  // 空白测试评论和本机服务断开时，都无需发送请求。
  manualSend.disabled = !serviceReady || !manualInput.value.trim();
}

export function setServiceReady(online) {
  serviceReady = online;
  manualInput.disabled = !online;
  syncManualSend();
  renderRoom();
}

async function updateRoom(room) {
  if (roomAction || !serviceReady) return;
  roomAction = room === null ? "stop" : "connect";
  roomError = "";
  renderRoom();
  try {
    // 成功状态由广播推进，避免 HTTP 响应晚到时覆盖更新的采集状态。
    await api("/api/workbench/room", "PUT", { room });
  } catch (e) {
    roomError = e.message;
  } finally {
    roomAction = null;
    renderRoom();
  }
}

$("room-form").addEventListener("submit", (e) => {
  e.preventDefault();
  // 回车提交也遵守禁用状态，避免重置正在连接、已连接或自动重连的房间。
  if (!roomConnect.disabled) updateRoom(parseRoom(roomInput.value.trim()));
});
roomStop.addEventListener("click", () => updateRoom(null));
roomInput.addEventListener("input", () => {
  roomError = "";
  renderRoom();
});
manualInput.addEventListener("input", syncManualSend);
setServiceReady(false);
loadLibrary();
loadModels();
readGeneration();
