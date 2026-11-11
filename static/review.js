// 文案审定页。所有文案内容都用 textContent / value 写入，不拼 HTML。
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const TYPES = { hook: "钩子", story: "故事", method: "方法", interaction: "互动", recap: "回顾" };
const TRUTH = { lived: "亲历", fictional: "虚构", opinion: "观点" };

let current = null;   // 当前文件名
let materials = {};   // 素材 id -> 素材
let dirty = false;

async function api(path, opts) {
  const r = await fetch(`/api/scripts${path}`, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function loadList() {
  const list = await api("");
  const box = $("#scripts");
  box.replaceChildren();
  if (!list.length) box.appendChild(el("p", "empty", "还没有生成的文案。运行 python -m server.script 生成。"));
  for (const s of list) {
    const b = el("button", `item${s.name === current ? " active" : ""}`);
    b.type = "button";
    const t = el("span", "t", s.title || s.name);
    t.appendChild(s.approved_at ? el("span", "badge ok", "已审定")
      : el("span", "badge warn", s.issues ? `待看 ${s.issues} 处` : "未审定"));
    b.appendChild(t);
    b.appendChild(el("span", "m", `${s.created_at || ""} · ${s.segments} 段 · ${s.chars} 字`));
    b.addEventListener("click", () => {
      if (dirty && !confirm("有未保存的修改，确定切换吗？")) return;
      open(s.name);
    });
    box.appendChild(b);
  }
}

async function open(name) {
  const data = await api(`/${encodeURIComponent(name)}`);
  current = name;
  materials = Object.fromEntries(data.materials.map((m) => [m.id, m]));
  $("#demo-warning").hidden = !data.demo_profile;
  render(data.script);
  setDirty(false);
  loadList();
}

function setDirty(v) {
  dirty = v;
  $("#dirty").hidden = !v;
}

function render(s) {
  $("#placeholder").hidden = true;
  $("#doc").hidden = false;
  $("#status").textContent = s.approved_at
    ? `已于 ${s.approved_at} 审定${s.approved_open_issues ? `（当时仍有 ${s.approved_open_issues} 处待看）` : ""}`
    : "未审定";
  $("#run-hint").textContent = `开播命令：.venv/bin/python -m server.app --script data/scripts/${current} --live-id 你的房间号`;
  for (const k of ["title", "bible", "big_hook", "payoff"]) $(`#f-${k}`).value = s[k] || "";
  fillIssues($("#outline-issues"), s.outline_issues);

  const box = $("#segments");
  box.replaceChildren();
  s.segments.forEach((seg, i) => {
    const sec = el("section", "seg");
    sec.dataset.id = seg.id;
    sec.appendChild(el("h2", null, `第 ${i + 1} 段 · ${seg.title}`));
    const r = seg.review || {};
    sec.appendChild(el("div", "meta",
      `计划 ${Math.round(seg.planned_sec / 60)} 分钟 · ${r.chars ?? "-"} 字（目标约 ${r.target_chars ?? "-"} 字）`));
    const ul = el("ul", "issues");
    fillIssues(ul, r.open_issues);
    sec.appendChild(ul);
    for (const p of seg.paragraphs) sec.appendChild(paraNode(p));
    box.appendChild(sec);
  });
}

function fillIssues(ul, issues) {
  ul.replaceChildren(...(issues || []).map((t) => el("li", null, t)));
}

function select(options, value) {
  const s = el("select");
  for (const [v, label] of Object.entries(options)) {
    const o = el("option", null, label);
    o.value = v;
    s.appendChild(o);
  }
  s.value = value;
  return s;
}

function paraNode(p) {
  const node = el("div", `para ${p.truth_level}`);
  node.dataset.id = p.id;
  const row = el("div", "row");
  row.appendChild(el("span", "id", p.id));
  const type = select(TYPES, p.type);
  const truth = select(TRUTH, p.truth_level);
  const mat = select({ "": "无素材", ...Object.fromEntries(Object.values(materials).map((m) => [m.id, `${m.id} ${m.title}`])) },
    p.material_id || "");
  if (p.material_id && !materials[p.material_id]) {   // 文案引用了档案里不存在的素材
    const o = el("option", null, `${p.material_id}（档案中不存在）`);
    o.value = p.material_id;
    mat.appendChild(o);
    mat.value = p.material_id;
  }
  const del = el("input");
  del.type = "checkbox";
  const delLabel = el("label", null);
  delLabel.append(del, "删除");
  const count = el("span", "count", `${p.text.length} 字`);
  row.append(type, truth, mat, delLabel, count);
  node.appendChild(row);

  const ta = el("textarea");
  ta.rows = Math.max(2, Math.ceil(p.text.length / 45));
  ta.value = p.text;
  node.appendChild(ta);

  const matBox = el("div", "material");
  node.appendChild(matBox);
  const refresh = () => {
    node.className = `para ${truth.value}${del.checked ? " deleted" : ""}`;
    count.textContent = `${ta.value.trim().length} 字`;
    const m = materials[mat.value];
    matBox.hidden = truth.value !== "lived";
    matBox.className = `material${m ? "" : " missing"}`;
    matBox.textContent = m ? `素材原文 [${m.id}]：${m.content}` : "标为亲历，但没有对应的真实素材：请选择素材，或改为观点 / 虚构";
  };
  for (const c of [type, truth, mat, del]) c.addEventListener("change", () => { refresh(); setDirty(true); });
  ta.addEventListener("input", () => { refresh(); setDirty(true); });
  refresh();
  return node;
}

function collect() {
  const body = {};
  for (const k of ["title", "bible", "big_hook", "payoff"]) body[k] = $(`#f-${k}`).value;
  body.segments = [...document.querySelectorAll(".seg")].map((sec) => ({
    id: sec.dataset.id,
    paragraphs: [...sec.querySelectorAll(".para")].map((n) => {
      const [type, truth, mat] = n.querySelectorAll("select");
      return {
        id: n.dataset.id, type: type.value, truth_level: truth.value, material_id: mat.value,
        text: n.querySelector("textarea").value, deleted: n.querySelector("input[type=checkbox]").checked,
      };
    }),
  }));
  return body;
}

async function save() {
  const data = await api(`/${encodeURIComponent(current)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(collect()),
  });
  render(data.script);
  setDirty(false);
  loadList();
  return data.script;
}

$("#save").addEventListener("click", async () => {
  try { await save(); } catch (e) { alert(`保存失败：${e.message}`); }
});

$("#approve").addEventListener("click", async () => {
  try {
    if (dirty) await save();
    const issues = document.querySelectorAll(".issues li").length;
    if (issues && !confirm(`还有 ${issues} 处待看的问题，确定审定通过吗？`)) return;
    const data = await api(`/${encodeURIComponent(current)}/approve`, { method: "POST" });
    render(data.script);
    loadList();
  } catch (e) { alert(`审定失败：${e.message}`); }
});

for (const k of ["title", "bible", "big_hook", "payoff"]) {
  $(`#f-${k}`).addEventListener("input", () => setDirty(true));
}

window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });

const wanted = new URLSearchParams(location.search).get("file");
loadList().then(() => wanted && open(wanted));
