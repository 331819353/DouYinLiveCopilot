import { Scroller } from "./scroller.js";

// 评论和模型输出都是不可信内容：一律用 textContent 写入，不拼 HTML。
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* 无痕模式等 */ } },
};

const MAX_COMMENTS = 500;
const MAX_CARDS = 5;
const CARD_TTL_MS = 3 * 60 * 1000;
const TYPE_LABEL = { hook: "钩子", story: "故事", method: "方法", interaction: "互动" };
const KIND_LABEL = { question: "提问", challenge: "质疑", resonance: "共鸣", off_topic: "跑题" };

let ws;
let script = null;
let scroller = null;
let startedAt = Date.now();
let segIndex = {};      // 段落 id -> 段序号
let segEnteredAt = null;
let currentSeg = null;
let viewportTimer = null;
let lastViewport = "";

// ---------------- WebSocket ----------------
let retryMs = 1000;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    setLamp("#src", "bad", "服务断开", "本机服务已断开，正在重连");
    setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 10000); // 服务长时间不在时，重连逐步放慢
  };
  ws.onopen = () => { retryMs = 1000; lastViewport = ""; sendViewport(); };
}
function send(msg) { if (ws?.readyState === 1) ws.send(JSON.stringify(msg)); }

function handle(m) {
  switch (m.type) {
    case "hello":
      startedAt = m.started * 1000;
      if (!script) renderScript(m.script);
      $("#approval").hidden = !!m.approved;
      $("#approval").title = `${m.script_file || ""}：开播前请在审定页通读并审定`;
      break;
    case "comment": queueComment(m); break;
    case "card": addCard(m.card); break;
    case "source": showSource(m); break;
    case "reply_status": showReply(m); break;
    case "stats": $("#online").textContent = `在线 ${m.online ?? "-"}`; break;
    case "insert_proposal": addProposal(m.proposal); break;
    case "insert_status": showInsertStatus(m); break;
    case "insert_expired": document.querySelector(`.card[data-id="${CSS.escape(m.id)}"]`)?.remove(); break;
    case "script_insert":
      renderScript(m.script); // 插入段只会在当前段之后，已读内容的位置不变
      document.querySelector(`.card.insert[data-seg="${CSS.escape(m.segment.id)}"]`)?.remove();
      break;
  }
}

// ---------------- 文案 ----------------
function renderScript(data) {
  script = data;
  const track = $("#script-track");
  track.replaceChildren();
  segIndex = {};
  const paras = [];
  data.segments.forEach((seg, i) => {
    const h = el("h2", seg.inserted ? "inserted" : null, `第 ${i + 1} 段 · ${seg.title}`);
    track.appendChild(h);
    for (const p of seg.paragraphs) {
      segIndex[p.id] = i;
      const node = el("p", `para ${p.type || ""}`);
      node.dataset.id = p.id;
      if (p.truth_level === "fictional") node.appendChild(el("span", "tag fictional", "虚构·需说明"));
      if (p.type === "interaction") node.appendChild(el("span", "tag interaction", "互动"));
      node.appendChild(document.createTextNode(p.text));
      node.title = TYPE_LABEL[p.type] || "";
      track.appendChild(node);
      paras.push(node);
    }
  });
  if (scroller) {  // 再次渲染（插入段后）：沿用现有滚动位置
    scroller.setParagraphs(paras);
    return;
  }
  const cpm = Number(store.get("cpm", 240)) || 240;
  scroller = new Scroller($("#script-viewport"), track, {
    cpm,
    onCurrent: onCurrentParagraph,
    onVisible: () => scheduleViewport(),
    onPause: (reason) => {
      const b = $("#pause-badge");
      b.hidden = !reason;
      b.textContent = reason || "";
    },
  });
  scroller.setParagraphs(paras);
  showSpeed();
}

function onCurrentParagraph(id) {
  const i = segIndex[id];
  if (i !== currentSeg) {
    currentSeg = i;
    segEnteredAt = Date.now();
    document.querySelectorAll(".card.insert").forEach((n) => n._checkStale?.());
  }
  scheduleViewport();
}

function scheduleViewport() {
  clearTimeout(viewportTimer);
  viewportTimer = setTimeout(sendViewport, 1000); // 防抖 1 秒
}
function sendViewport() {
  if (!scroller) return;
  const msg = { type: "viewport", visible: [...scroller.visible], reading: scroller.currentId };
  const key = JSON.stringify(msg);
  if (key === lastViewport) return;
  lastViewport = key;
  send(msg);
}

function showSpeed() { $("#speed").textContent = `${scroller.cpm} 字/分`; }

const fmt = (sec) => {
  const s = Math.max(0, Math.floor(sec));
  return [s / 3600, (s % 3600) / 60, s % 60].map((v) => String(Math.floor(v)).padStart(2, "0")).join(":");
};
setInterval(() => {
  $("#clock").textContent = fmt((Date.now() - startedAt) / 1000);
  if (script && currentSeg != null) {
    const seg = script.segments[currentSeg];
    const left = (seg.planned_sec || 0) - (Date.now() - segEnteredAt) / 1000;
    const leftText = left >= 0 ? `剩 ${fmt(left).slice(3)}` : `超时 ${fmt(-left).slice(3)}`;
    $("#seg").textContent = `第 ${currentSeg + 1}/${script.segments.length} 段 · ${seg.title} · ${leftText}`;
  }
}, 500);

// ---------------- 快捷键 ----------------
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea") || !scroller) return;
  if (e.code === "Space") {
    e.preventDefault();
    scroller.toggleLock();
  } else if (e.key === "+" || e.key === "=") {
    scroller.setSpeed(scroller.cpm + 10);
  } else if (e.key === "-" || e.key === "_") {
    scroller.setSpeed(scroller.cpm - 10);
  } else {
    return;
  }
  store.set("cpm", scroller.cpm);
  showSpeed();
});

// ---------------- 评论 ----------------
const commentsBox = $("#comments");
const pill = $("#new-pill");
const seenComments = new Set();
let pending = [];
let hoverComments = false;
let unseen = 0;

commentsBox.addEventListener("pointerenter", () => { hoverComments = true; });
commentsBox.addEventListener("pointerleave", () => { hoverComments = false; });
pill.addEventListener("click", () => {
  commentsBox.scrollTop = commentsBox.scrollHeight;
  unseen = 0;
  pill.hidden = true;
});
$("#show-filtered").addEventListener("change", (e) => {
  commentsBox.classList.toggle("show-filtered", e.target.checked);
});
$("#manual").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#manual-input");
  const text = input.value.trim();
  if (text) send({ type: "manual_comment", text });
  input.value = "";
});

function queueComment(c) {
  if (seenComments.has(c.id)) return;
  seenComments.add(c.id);
  pending.push(c);
}

// 每 250 毫秒批量渲染一次，高峰期不卡
setInterval(() => {
  if (!pending.length) return;
  const frag = document.createDocumentFragment();
  let visibleCount = 0;
  for (const c of pending) {
    const hidden = c.filtered && c.filtered !== "数字";
    const row = el("div", "c");
    row.dataset.id = c.id;
    if (hidden) row.classList.add("filtered");
    if (c.q) row.classList.add("q");
    row.appendChild(el("span", "nick", c.nick || "观众"));
    row.appendChild(el("span", "text", c.text));
    if (c.filtered) row.appendChild(el("span", "why", c.filtered));
    if (!hidden) {
      const gen = el("button", "gen", "生成回复");
      gen.type = "button";
      gen.addEventListener("click", () => send({ type: "reply_now", comment_id: c.id }));
      row.appendChild(gen);
      visibleCount++;
    }
    frag.appendChild(row);
  }
  pending = [];
  commentsBox.appendChild(frag);
  while (commentsBox.children.length > MAX_COMMENTS) commentsBox.firstElementChild.remove();
  if (hoverComments) {
    unseen += visibleCount;
    if (unseen) {
      pill.textContent = `↓ ${unseen} 条新评论`;
      pill.hidden = false;
    }
  } else {
    commentsBox.scrollTop = commentsBox.scrollHeight;
    unseen = 0;
    pill.hidden = true;
  }
}, 250);

// ---------------- 回复卡片 ----------------
const cardsBox = $("#cards");
const seenCards = new Set();

function addCard(card) {
  if (seenCards.has(card.id)) return;
  seenCards.add(card.id);
  $("#cards-empty")?.remove();
  const esc = card.risk === "escalate";
  const node = el("div", `card${esc ? " escalate" : ""}`);
  node.dataset.id = card.id;
  node.dataset.t = Date.now();

  const meta = el("div", "meta");
  meta.appendChild(el("span", "kind", esc ? "需要主播处理" : KIND_LABEL[card.kind] || card.kind));
  if (card.risk === "sensitive") meta.appendChild(el("span", null, "敏感"));
  if (card.manual) meta.appendChild(el("span", null, "手动生成"));
  meta.appendChild(el("span", null, `${(card.latency_ms / 1000).toFixed(1)} 秒`));
  node.appendChild(meta);

  node.appendChild(el("div", "src", card.comments.map((c) => `${c.nick || "观众"}：${c.text}`).join(" / ")));
  node.appendChild(el("div", "reply", card.reply));
  if (esc && card.help) node.appendChild(el("div", "help", card.help));

  if (!esc) {
    const more = el("div", "more");
    if (card.expand) {
      const text = card.expand.length > 120 ? `${card.expand.slice(0, 120)}…` : card.expand;
      const p = el("p");
      p.appendChild(el("span", "label", "展开"));
      p.appendChild(document.createTextNode(text));
      more.appendChild(p);
    }
    for (const [label, value] of [["抛问题", card.discussion_question], ["拉回", card.bridge_back]]) {
      if (!value) continue;
      const p = el("p");
      p.appendChild(el("span", "label", label));
      p.appendChild(document.createTextNode(value));
      more.appendChild(p);
    }
    if (card.anchor_quote) more.appendChild(el("p", "anchor", `依据文案：「${card.anchor_quote}」`));
    node.appendChild(more);
  }

  const actions = el("div", "actions");
  const btn = (label, fn, cls) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", fn);
    actions.appendChild(b);
    return b;
  };
  const holdKey = `card:${card.id}`;
  const report = (action) => send({ type: "card_action", id: card.id, action }); // 供下播复盘统计
  if (!esc) {
    const toggle = btn("展开", () => {
      const open = node.classList.toggle("open");
      toggle.textContent = open ? "收起" : "展开";
      if (open) scroller?.hold(holdKey); else scroller?.release(holdKey); // 看卡片时文案暂停
      report(open ? "expand" : "collapse");
    }, "primary");
  }
  btn(esc ? "已处理" : "已用", () => {
    node.classList.add("used");
    scroller?.release(holdKey);
    report("used");
    setTimeout(() => node.remove(), 1500);
  });
  const pin = btn("置顶", () => {
    const pinned = node.classList.toggle("pinned");
    pin.textContent = pinned ? "取消置顶" : "置顶";
    report(pinned ? "pin" : "unpin");
  });
  if (!esc) btn("忽略", () => { scroller?.release(holdKey); report("dismiss"); node.remove(); });
  node.appendChild(actions);

  cardsBox.prepend(node);
  for (const c of card.comments) {
    commentsBox.querySelector(`.c[data-id="${CSS.escape(c.id)}"]`)?.classList.add("cited");
  }
  trimCards();
}

// 未置顶、没在看的卡片最多保留 5 张，超过 3 分钟自动移除
function trimCards() {
  const now = Date.now();
  const free = [...cardsBox.querySelectorAll(".card")].filter(
    (n) => !n.classList.contains("pinned") && !n.classList.contains("open") && !n.matches(":hover")
      && !n.classList.contains("insert"));  // 插入段提案由服务端控制过期
  free.forEach((n, i) => {
    if (i >= MAX_CARDS || now - Number(n.dataset.t) > CARD_TTL_MS) n.remove();
  });
}
setInterval(trimCards, 5000);

// ---------------- 插入段 ----------------
const TRUTH_LABEL = { lived: "亲历", fictional: "虚构", opinion: "观点" };
const insertBtn = $("#insert-btn");
insertBtn.addEventListener("click", () => send({ type: "insert_now" }));

function addProposal(p) {
  if (document.querySelector(`.card[data-id="${CSS.escape(p.id)}"]`)) return;
  $("#cards-empty")?.remove();
  const node = el("div", "card insert");
  node.dataset.id = p.id;
  node.dataset.seg = p.segment.id;
  const meta = el("div", "meta");
  meta.appendChild(el("span", "kind", "建议插入段"));
  meta.appendChild(el("span", null, `约 ${Math.round(p.segment.planned_sec / 60)} 分钟 · ${p.segment.paragraphs.length} 段`));
  node.appendChild(meta);
  node.appendChild(el("div", "reply", p.theme));
  if (p.why) node.appendChild(el("div", "src", p.why));
  node.appendChild(el("div", "src", `观众在说：${p.comments.slice(0, 3).join(" / ")}`));
  for (const s of p.issues || []) node.appendChild(el("div", "issue", `⚠ ${s}`));
  const stale = el("div", "issue stale");
  stale.hidden = true;
  node.appendChild(stale);
  node._checkStale = () => {  // 写这段时所在的段落和现在不同，提醒主播检查衔接
    const now = script?.segments[currentSeg];
    stale.hidden = !p.written_at || !now || now.id === p.written_at;
    stale.textContent = `⚠ 写这段时你在「${p.written_at_title}」，现在已到「${now?.title || ""}」，插入前留意衔接`;
  };
  node._checkStale();

  const more = el("div", "more");
  for (const q of p.segment.paragraphs) {
    const line = el("p");
    line.appendChild(el("span", "label", TRUTH_LABEL[q.truth_level] || ""));
    line.appendChild(document.createTextNode(q.text));
    more.appendChild(line);
  }
  node.appendChild(more);

  const actions = el("div", "actions");
  const btn = (label, fn, cls) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", fn);
    actions.appendChild(b);
    return b;
  };
  const holdKey = `insert:${p.id}`;
  const preview = btn("预览", () => {
    const open = node.classList.toggle("open");
    preview.textContent = open ? "收起" : "预览";
    if (open) scroller?.hold(holdKey); else scroller?.release(holdKey);
  }, "primary");
  btn("插入到下一段前", () => {
    scroller?.release(holdKey);
    send({ type: "insert_accept", id: p.id });
  });
  btn("忽略", () => {
    scroller?.release(holdKey);
    send({ type: "insert_dismiss", id: p.id });
    node.remove();
  });
  node.appendChild(actions);
  cardsBox.prepend(node);
}

function showInsertStatus(m) {
  const busy = m.state === "busy";
  insertBtn.disabled = m.state !== "idle";
  insertBtn.textContent = busy ? "插入段生成中…" : m.state === "pending" ? "有待选插入段" : "生成插入段";
  insertBtn.title = m.note || (m.state === "disabled" ? "AI 未启用" : "根据最近 3 分钟的评论生成一段临时内容");
  if (m.note && m.state !== "disabled") $("#ai-note").textContent = m.note;
}

// ---------------- 状态 ----------------
function setLamp(sel, level, text, title) {
  const n = $(sel);
  n.className = `lamp ${level}`;
  n.textContent = text;
  n.title = title || "";
}

const SOURCE_TEXT = {
  ok: ["ok", "采集正常"], quiet: ["warn", "采集可能静默"], dead: ["bad", "连接可能假死"],
  reconnecting: ["warn", "采集重连中"], starting: ["busy", "采集连接中"], ended: ["bad", "直播已结束"],
  stopped: ["bad", "采集已停止"], manual: ["warn", "手动模式"],
};
function showSource(m) {
  const [level, text] = SOURCE_TEXT[m.state] || ["warn", m.state];
  const label = m.kind === "mock" && m.state === "ok" ? "模拟评论" : text;
  setLamp("#src", level, label, `${m.note || ""}${m.restarts ? `（已重连 ${m.restarts} 次）` : ""}`);
}

const AI_TEXT = { idle: ["ok", "AI 就绪"], busy: ["busy", "AI 生成中"], error: ["warn", "AI 出错"],
  disabled: ["bad", "AI 未启用"] };
function showReply(m) {
  const [level, text] = AI_TEXT[m.state] || ["warn", m.state];
  const p50 = m.p50_ms ? ` · 中位 ${(m.p50_ms / 1000).toFixed(1)} 秒` : "";
  setLamp("#ai", level, text, `${m.provider} · 调用 ${m.calls} 次 · 卡片 ${m.cards} · 丢弃 ${m.dropped}${p50}`);
  if (m.note) $("#ai-note").textContent = m.note;
}

connect();
