// 模型清单沿用后端已有的模型条目 ID。供应商只负责分组，默认用途仍引用具体模型。
export function initModelSettings({ api, note, make, onUpdate }) {
  const $ = (id) => document.getElementById(id);
  const endpoint = "/api/workbench/models";
  const roles = { script: "默认文案", reply: "实时解答" };
  const serviceMarks = { DEEPSEEK_API_KEY: "DS", OPENAI_API_KEY: "OA", ARK_API_KEY: "方" };
  const registry = $("model-settings");
  const search = $("model-search");
  const defaultsOpen = $("model-defaults-open");
  const defaultsDialog = $("model-defaults");
  const defaultsForm = $("model-defaults-form");
  const keyDialog = $("model-key");
  const keyForm = $("model-key-form");
  const keyInput = $("model-key-input");
  const editor = $("model-editor");
  const editorForm = $("model-editor-form");
  const modelId = $("model-id");
  const compatibleFields = $("model-compatible-fields");
  const baseUrl = $("model-base-url");
  const compatibleKey = $("model-compatible-key");
  const test = $("model-test");
  const add = $("model-add");
  const catalogLoad = $("model-catalog-load");
  const catalogSearch = $("model-catalog-search");
  const catalogSelect = $("model-catalog-select");
  const groups = new Map();
  const expanded = new Set();
  let data;
  let keyGroup;
  let editorGroup;
  let editorEntry;
  let catalog = [];
  let catalogRequest = 0;
  let catalogLoading = false;
  let testRequest = 0;
  let testLoading = false;
  let settingsRequest = 0;

  function button(label, classes = "control") {
    const item = make("button", label, classes);
    item.type = "button";
    return item;
  }

  function renderProvider(service, entries, filtering) {
    const { id, info } = service;
    const article = make("article", null, "model-provider");
    article.dataset.provider = id;
    article.dataset.service = info.key_env;
    const header = make("div", null, "model-provider-head");
    const identity = make("div", null, "model-provider-identity");
    const mark = make("span", info.compatible ? "API" : serviceMarks[info.key_env] || info.service.slice(0, 2), "model-provider-mark");
    mark.setAttribute("aria-hidden", "true");
    const details = make("div");
    details.append(make("h4", info.service), make("p", info.compatible ? "自定义 API 地址" : "对话模型"));
    identity.append(mark, details);
    const credentials = make("div", null, "model-provider-credentials");
    const ready = service.models.some((entry) => entry.ready);
    const keyState = info.compatible ? (ready ? "模型已配置" : "待添加模型")
      : { local: "密钥已保存", env: "使用环境变量密钥" }[info.key_source]
        || (info.has_key ? "密钥已配置" : "待配置密钥");
    const status = make("span", keyState, "model-key-state");
    status.dataset.ready = info.compatible ? ready : info.has_key;
    credentials.append(status);
    if (!info.compatible) {
      const key = button(info.has_key ? "管理密钥" : "添加 API 密钥", info.has_key ? "control" : "control primary-control");
      key.dataset.action = "key";
      key.setAttribute("aria-haspopup", "dialog");
      key.addEventListener("click", () => openKey(service));
      credentials.append(key);
    }
    header.append(identity, credentials);
    article.append(header);

    const footer = make("div", null, "model-provider-actions");
    const list = make("ul", null, "model-list");
    list.id = "models-" + id;
    if (entries.length) {
      const toggle = button("", "control model-toggle");
      const toggleLabel = make("span");
      toggle.append(toggleLabel, make("span", String(entries.length), "model-count"));
      toggle.setAttribute("aria-controls", list.id);
      const setOpen = (open) => {
        list.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggleLabel.textContent = open ? "收起模型" : "显示模型";
      };
      setOpen(filtering || expanded.has(id));
      toggle.addEventListener("click", () => {
        const open = list.hidden;
        if (open) expanded.add(id);
        else expanded.delete(id);
        setOpen(open);
      });
      footer.append(toggle);
    } else {
      footer.append(make("span", "尚未添加模型", "form-note"));
    }
    const create = button("添加模型", info.compatible && !ready ? "control primary-control" : "control model-link");
    create.dataset.action = "add";
    create.setAttribute("aria-haspopup", "dialog");
    create.addEventListener("click", () => openEditor(service));
    footer.append(create);
    article.append(footer);
    for (const model of entries) {
      const row = make("li", null, "model-list-row");
      const content = make("div");
      content.append(make("h5", model.compatible ? model.model : model.label));
      const detail = model.compatible ? model.base_url : model.label !== model.model ? model.model : "";
      if (detail) content.append(make("p", detail, "model-id-text"));
      const usage = make("div", null, "model-usage");
      for (const [name, label] of Object.entries(roles)) {
        if (data.routes[name] === model.id) usage.append(make("span", label, "model-role"));
      }
      content.append(usage);
      const verify = button(model.compatible ? "编辑 / 测试" : "测试连接");
      verify.dataset.action = model.id;
      verify.setAttribute("aria-label", `${model.compatible ? "编辑或测试" : "测试连接"} · ${model.label}`);
      verify.setAttribute("aria-haspopup", "dialog");
      verify.addEventListener("click", () => openEditor(service, model));
      row.append(content, verify);
      list.append(row);
    }
    if (entries.length) article.append(list);
    return article;
  }

  function renderRegistry() {
    const root = $("model-registry");
    const query = search.value.trim().toLowerCase();
    const sections = { ready: [], pending: [] };
    for (const service of groups.values()) {
      // 搜供应商时保留该供应商的全部模型，搜模型时只展开命中的条目。
      const providerMatch = service.info.service.toLowerCase().includes(query);
      const entries = providerMatch ? service.models : service.models.filter((item) =>
        [item.label, item.model].some((value) => value.toLowerCase().includes(query)));
      if (!providerMatch && !entries.length) continue;
      sections[service.models.some((item) => item.ready) ? "ready" : "pending"].push(
        renderProvider(service, entries, Boolean(query)));
    }
    root.replaceChildren();
    if (!sections.ready.length && !sections.pending.length) {
      root.append(make("p", query ? "没有匹配的供应商或模型。" : "暂无模型供应商。", "model-empty"));
      return;
    }
    for (const [state, title] of [["ready", "已配置"], ["pending", "待配置"]]) {
      if (!sections[state].length) continue;
      const section = make("section", null, "model-provider-section");
      section.append(make("h3", title), ...sections[state]);
      root.append(section);
    }
  }

  function apply(next) {
    data = next;
    groups.clear();
    for (const item of data.providers) {
      if (!groups.has(item.catalog_group)) {
        groups.set(item.catalog_group, { id: item.catalog_group, info: item, models: [] });
      }
      if (item.model) groups.get(item.catalog_group).models.push(item);
    }
    onUpdate(data);
    search.disabled = false;
    defaultsOpen.disabled = false;
    renderRegistry();
  }

  $("model-settings-open").addEventListener("click", async () => {
    const request = ++settingsRequest;
    registry.showModal();
    search.value = "";
    search.disabled = defaultsOpen.disabled = true;
    $("model-registry").replaceChildren();
    note("model-settings-status", "正在读取模型设置…");
    try {
      const next = await api(endpoint);
      if (!registry.open || request !== settingsRequest) return;
      apply(next);
      note("model-settings-status", "");
    } catch (error) {
      if (registry.open && request === settingsRequest) note("model-settings-status", error.message, true);
    }
  });
  search.addEventListener("input", renderRegistry);

  // 使用原生对话框处理焦点圈和 Esc，写入期间锁定操作，避免产生重复提交。
  for (const dialog of [registry, defaultsDialog, keyDialog, editor]) {
    dialog.querySelectorAll("[data-model-close]").forEach((item) => {
      item.addEventListener("click", () => dialog.close());
    });
    dialog.addEventListener("cancel", (event) => {
      if (dialog.querySelector("fieldset")?.disabled) event.preventDefault();
    });
  }

  async function save(dialog, status, payload, message, target) {
    const fields = dialog.querySelector("fieldset");
    if (fields.disabled) return;
    fields.disabled = true;
    const close = dialog.querySelector(".model-close");
    close.disabled = true;
    note(status, "正在保存…");
    try {
      const next = await api(endpoint, "PUT", payload);
      if (dialog === keyDialog) keyInput.value = "";
      if (dialog === editor) compatibleKey.value = "";
      dialog.close();
      apply(next);
      // 卡片因保存而重新渲染，恢复到新的同位置按钮。筛选结果消失时回到搜索框。
      (target?.() || search).focus();
      note("model-settings-status", message);
    } catch (error) {
      note(status, error.message, true);
    } finally {
      fields.disabled = close.disabled = false;
    }
  }

  function cardAction(group, action) {
    return $("model-registry").querySelector(
      `[data-provider="${CSS.escape(group)}"] [data-action="${CSS.escape(action)}"]`);
  }

  function defaultNote(name) {
    const field = defaultsForm.elements[name];
    const entry = data.providers.find((model) => model.id === field.value);
    const detail = entry?.ready ? (entry.base_url || "") : (entry
      ? "当前模型待配置，请在供应商清单中补齐密钥和模型 ID。"
      : "请先添加模型并配置 API 密钥。");
    note(`model-default-${name}-note`, detail, !entry?.ready);
  }

  defaultsOpen.addEventListener("click", () => {
    for (const name of Object.keys(roles)) {
      const field = defaultsForm.elements[name];
      const current = data.routes[name];
      const options = [];
      for (const service of groups.values()) {
        const group = make("optgroup");
        group.label = service.info.service;
        for (const entry of data.providers.filter((item) => item.catalog_group === service.id)) {
          if (entry.id !== current && !entry.ready) continue;
          // 原生下拉框收起时只显示选项文字，给自定义模型保留供应商名以区分同名模型。
          const name = entry.compatible ? entry.label
            : entry.label.startsWith(service.info.service)
            ? entry.label : `${service.info.service} / ${entry.label}`;
          const label = name + (entry.ready ? "" : " · 待配置");
          group.append(new Option(label, entry.id));
        }
        if (group.children.length) options.push(group);
      }
      field.replaceChildren(...options);
      if (![...field.options].some((option) => option.value === current)) {
        field.prepend(new Option("请选择模型", ""));
      }
      field.value = [...field.options].some((option) => option.value === current) ? current : "";
      defaultNote(name);
    }
    note("model-defaults-status", "");
    defaultsDialog.showModal();
    defaultsForm.elements.script.focus();
  });

  for (const name of Object.keys(roles)) {
    defaultsForm.elements[name].addEventListener("change", () => defaultNote(name));
  }
  defaultsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const routes = Object.fromEntries(Object.keys(roles).map((name) => [name, defaultsForm.elements[name].value]));
    save(defaultsDialog, "model-defaults-status", { routes }, "默认模型已保存。", () => defaultsOpen);
  });

  function openKey(service) {
    const { info } = service;
    keyGroup = service;
    $("model-key-title").textContent = info.has_key ? "管理 API 密钥" : "添加 API 密钥";
    $("model-key-description").textContent = `${info.service}的模型共用这项凭据。保存后，本机设置优先于环境变量。`;
    keyInput.value = "";
    keyInput.placeholder = info.has_key ? "输入新密钥以替换当前凭据" : "输入供应商 API 密钥";
    note("model-key-status", "");
    keyDialog.showModal();
    keyInput.focus();
  }

  keyDialog.addEventListener("close", () => {
    if (!keyDialog.open) keyInput.value = ""; // 关闭后清除明文输入，任何接口都不回填已有密钥。
  });
  keyForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const key = keyInput.value.trim();
    if (!key) {
      note("model-key-status", "请输入 API 密钥。", true);
      keyInput.focus();
      return;
    }
    const { id, info } = keyGroup;
    save(keyDialog, "model-key-status", { keys: { [info.key_env]: key } },
      `${info.service}的 API 密钥已保存。`, () => cardAction(id, "key"));
  });

  function updateEditor() {
    const value = modelId.value.trim();
    if (editorGroup.info.compatible) {
      const hasValues = Boolean(baseUrl.value.trim() && value
        && (compatibleKey.value.trim() || editorEntry?.has_key));
      test.disabled = !hasValues || testLoading;
      add.disabled = !hasValues || Boolean(editorEntry && !compatibleKey.value.trim());
      return;
    }
    const existing = !editorEntry && editorGroup.models.some((model) => model.model === value);
    const hasKey = editorGroup.info.has_key;
    catalogLoad.disabled = !hasKey || catalogLoading;
    test.disabled = !value || !hasKey || testLoading;
    add.disabled = !value || existing;
    note("model-editor-note", existing ? "此模型已在清单中，可直接测试连接。"
      : hasKey ? "" : "请先保存供应商的 API 密钥，再获取目录或测试连接。");
  }

  function clearTest() {
    // 输入或窗口切换后丢弃尚未返回的旧响应，避免把别的模型标为测试成功。
    testRequest++;
    testLoading = false;
    test.textContent = "测试连接";
    $("model-test-note").dataset.success = "false";
    note("model-test-note", "");
  }

  function openEditor(service, entry = null) {
    editorGroup = service;
    editorEntry = entry;
    const compatible = Boolean(service.info.compatible);
    $("model-editor-title").textContent = entry ? (compatible ? "编辑兼容模型" : "测试模型连接") : "添加模型";
    $("model-editor-description").textContent = entry && !compatible
      ? `${service.info.service} · ${entry.label}` : service.info.service;
    compatibleFields.hidden = !compatible;
    baseUrl.value = entry?.base_url || "";
    baseUrl.required = compatible;
    baseUrl.readOnly = Boolean(entry && compatible);
    compatibleKey.value = "";
    compatibleKey.required = compatible && !entry?.has_key;
    $("model-base-url-hint").textContent = entry && compatible
      ? "要使用其他地址或模型 ID，请返回供应商卡片添加模型。"
      : "填写服务商提供的 API 基础地址，例如以 /v1 结尾的地址。";
    $("model-compatible-key-hint").textContent = entry?.has_key ? "留空继续使用已保存的密钥。" : "";
    modelId.placeholder = compatible ? "填写服务商提供的模型 ID" : "手动填写，或从目录选择";
    modelId.value = entry?.model || "";
    modelId.readOnly = Boolean(entry);
    add.textContent = entry && compatible ? "保存密钥" : "添加模型";
    add.hidden = Boolean(entry && !compatible);
    $("model-catalog-tools").hidden = compatible || Boolean(entry);
    catalogRequest++;
    catalogLoading = false;
    catalog = [];
    catalogSearch.value = "";
    catalogSelect.replaceChildren();
    catalogLoad.textContent = "获取供应商目录";
    $("model-catalog").hidden = true;
    note("model-catalog-note", "");
    note("model-editor-status", "");
    note("model-editor-note", "");
    clearTest();
    updateEditor();
    editor.showModal();
    (compatible ? (entry ? compatibleKey : baseUrl) : (entry && !test.disabled ? test : modelId)).focus();
  }

  editor.addEventListener("close", () => {
    if (!editor.open) compatibleKey.value = ""; // 离开弹窗后清除尚未保存的密钥。
  });

  function renderCatalog() {
    const query = catalogSearch.value.trim().toLowerCase();
    const visible = catalog.filter((id) => id.toLowerCase().includes(query));
    const registered = new Set(editorGroup.models.map((entry) => entry.model));
    catalogSelect.replaceChildren(...visible.map((id) => new Option(registered.has(id) ? id + " · 已添加" : id, id)));
    catalogSelect.selectedIndex = -1;
    note("model-catalog-result", visible.length ? `显示 ${visible.length} / ${catalog.length} 个模型`
      : query ? "没有匹配的模型。" : "目录未返回模型，可手动填写模型 ID。");
  }

  catalogSearch.addEventListener("input", renderCatalog);
  for (const field of [catalogSearch, catalogSelect]) {
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) event.preventDefault();
    });
  }
  catalogSelect.addEventListener("change", () => {
    modelId.value = catalogSelect.value;
    clearTest();
    updateEditor();
  });
  catalogLoad.addEventListener("click", async () => {
    const request = ++catalogRequest;
    catalogLoading = catalogLoad.disabled = true;
    catalogLoad.textContent = "获取中…";
    note("model-catalog-note", "正在读取供应商模型目录…");
    try {
      const response = await api(endpoint + "/catalog", "POST", { provider: editorGroup.info.base });
      if (!editor.open || request !== catalogRequest) return;
      catalog = response.models;
      $("model-catalog").hidden = false;
      renderCatalog();
      note("model-catalog-note", "");
    } catch (error) {
      if (editor.open && request === catalogRequest) note("model-catalog-note", error.message, true);
    } finally {
      if (editor.open && request === catalogRequest) {
        catalogLoading = catalogLoad.disabled = false;
        catalogLoad.textContent = "获取供应商目录";
      }
    }
  });

  for (const field of [modelId, baseUrl, compatibleKey]) {
    field.addEventListener("input", () => {
      clearTest();
      note("model-editor-status", "");
      updateEditor();
    });
  }

  function validId() {
    const value = modelId.value.trim();
    return /^[A-Za-z0-9._:/-]{1,150}$/.test(value) ? value : "";
  }

  function compatiblePayload(model, status) {
    if (!baseUrl.reportValidity() || !compatibleKey.reportValidity()) return;
    const address = baseUrl.value.trim();
    if (!/^https?:\/\//i.test(address)) {
      note(status, "请填写以 http:// 或 https:// 开头的 API 地址。", true);
      baseUrl.focus();
      return;
    }
    const key = compatibleKey.value.trim();
    // 更新已有模型时，空字段由服务端沿用原密钥；表单从不读取该密钥。
    return { base_url: address, model, ...(key ? { api_key: key } : {}) };
  }

  test.addEventListener("click", async () => {
    const model = validId();
    if (!model) {
      note("model-test-note", "模型 ID 仅可包含字母、数字与 . _ : / -。", true);
      return;
    }
    const compatible = editorGroup.info.compatible;
    const details = compatible ? compatiblePayload(model, "model-test-note") : { model };
    if (!details) return;
    const request = ++testRequest;
    testLoading = test.disabled = true;
    test.textContent = "测试中…";
    note("model-test-note", "正在请求模型服务…");
    // 已添加的预设使用自己的推理参数，新增 ID 由后端套用供应商的通用参数。
    const matched = editorEntry || (!compatible && editorGroup.models.find((entry) => entry.model === model));
    try {
      const response = await api(endpoint + "/test", "POST", { provider: matched?.id || editorGroup.info.base, ...details });
      if (!editor.open || request !== testRequest) return;
      $("model-test-note").dataset.success = "true";
      note("model-test-note", `连接成功 · ${(response.elapsed_ms / 1000).toFixed(2)} 秒`);
    } catch (error) {
      if (editor.open && request === testRequest) note("model-test-note", error.message, true);
    } finally {
      if (editor.open && request === testRequest) {
        testLoading = false;
        test.textContent = "测试连接";
        updateEditor();
      }
    }
  });

  editorForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (add.hidden || add.disabled) return;
    const model = validId();
    if (!model) {
      note("model-editor-status", "模型 ID 仅可包含字母、数字与 . _ : / -。", true);
      modelId.focus();
      return;
    }
    const { id, info } = editorGroup;
    let payload = { additions: [{ provider: info.base, model }] };
    if (info.compatible) {
      const details = compatiblePayload(model, "model-editor-status");
      if (!details) return;
      payload = { compatible: editorEntry ? { ...details, id: editorEntry.id } : details };
    }
    expanded.add(id);
    save(editor, "model-editor-status", payload, editorEntry ? "API 密钥已更新。"
      : `${info.service} 已添加模型 ${model}。`, () => cardAction(id, editorEntry?.id || "add"));
  });
}
