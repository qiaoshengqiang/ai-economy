/* AIEOS 设置面板 —— 系统设置 / 常规设置 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const modal = $("settings-modal");
  const nav = $("settings-nav");
  const content = document.querySelector(".settings-content");
  if (!modal || !nav) return;

  let data = null;
  let currentPane = "system-providers";
  let editing = { provider: "", model: "", credential: "", role: "" };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let payload = null;
    try { payload = await response.json(); } catch (e) { payload = null; }
    if (!response.ok || (payload && payload.ok === false)) {
      throw new Error(
        (payload && (payload.error || payload.message)) ||
        `HTTP ${response.status}`);
    }
    return payload;
  }

  function toast(text, isError = false) {
    const node = $("toast");
    if (!node) return;
    node.textContent = String(text || "");
    node.classList.toggle("error", !!isError);
    node.hidden = false;
    clearTimeout(node.__timer);
    node.__timer = setTimeout(() => { node.hidden = true; }, 6000);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function option(select, value, label, selected = false) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label || value;
    node.selected = selected;
    select.appendChild(node);
    return node;
  }

  function clear(node) { node.innerHTML = ""; }

  function paneButton(pane) {
    return nav.querySelector(`[data-pane="${pane}"]`);
  }

  function showPane(pane) {
    currentPane = pane;
    nav.querySelectorAll("[data-pane]").forEach((button) => {
      button.classList.toggle("active", button.dataset.pane === pane);
    });
    content.querySelectorAll(".settings-pane").forEach((section) => {
      section.hidden = section.dataset.pane !== pane;
    });
  }

  async function load() {
    data = await api("/api/settings");
    renderAll();
    // 每次打开 / 保存后回到“新建”表单，避免上一次编辑状态残留。
    editing = { provider: "", model: "", credential: "", role: "" };
    newProvider();
    newModel();
    newCredential();
    newRole();
  }

  async function open() {
    try {
      await load();
      modal.hidden = false;
      showPane(currentPane);
    } catch (error) {
      toast(`设置读取失败：${error.message}`, true);
    }
  }

  function close() { modal.hidden = true; }

  document.getElementById("settings-btn").addEventListener("click", open);
  $("settings-close").addEventListener("click", close);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) close();
  });
  nav.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pane]");
    if (button) showPane(button.dataset.pane);
  });

  // ------------------------------------------------------------
  // render
  // ------------------------------------------------------------
  function renderAll() {
    const system = data.system || {};
    $("system-restart-banner").hidden = !system.restart_required;
    renderProviders();
    renderModels();
    renderCredentials();
    renderSystemInfo();
    renderRuntime();
    renderAgents();
    renderRoles();
  }

  function card(title, meta, actions) {
    const box = el("div", "settings-item");
    const body = el("div", "settings-item-body");
    body.appendChild(el("div", "settings-item-title", title));
    if (meta) body.appendChild(el("div", "settings-item-meta", meta));
    box.appendChild(body);
    const tools = el("div", "settings-item-actions");
    actions.forEach(([label, handler, danger]) => {
      const button = el("button", `mini-btn${danger ? " danger" : ""}`, label);
      button.type = "button";
      button.addEventListener("click", handler);
      tools.appendChild(button);
    });
    box.appendChild(tools);
    return box;
  }

  function renderProviders() {
    const list = $("provider-list");
    const providers = (data.system && data.system.providers) || {};
    clear(list);
    const names = Object.keys(providers);
    if (!names.length) {
      list.appendChild(el("div", "empty", "还没有服务商。"));
    }
    names.forEach((name) => {
      const raw = providers[name] || {};
      list.appendChild(card(
        name,
        `adapter ${raw.adapter || "?"} · ${raw.base_url || "(no base_url)"}`,
        [
          ["编辑", () => editProvider(name)],
          ["删除", () => deleteProvider(name), true],
        ]));
    });
    const select = $("provider-adapter");
    clear(select);
    const adapters = (data.system && data.system.adapters) || [];
    adapters.forEach((item) => option(
      select, item.name,
      `${item.name}${item.requires_base_url ? "" : " (no base_url)"}`));
  }

  function renderModels() {
    const list = $("model-list");
    const models = (data.system && data.system.models) || {};
    clear(list);
    const profiles = Object.keys(models);
    if (!profiles.length) {
      list.appendChild(el("div", "empty", "还没有模型。"));
    }
    profiles.forEach((profile) => {
      const raw = models[profile] || {};
      list.appendChild(card(
        profile,
        `${raw.provider || "?"} · ${raw.model_id || "?"} · ` +
          `context ${raw.context_window || "?"} / output ${
            raw.max_output_tokens || "?"}`,
        [
          ["编辑", () => editModel(profile)],
          ["删除", () => deleteModel(profile), true],
        ]));
    });
    const providerSelect = $("model-provider");
    clear(providerSelect);
    Object.keys((data.system && data.system.providers) || {}).forEach(
      (name) => option(providerSelect, name, name));
  }

  function renderCredentials() {
    const list = $("credential-list");
    const credentials = (data.system && data.system.credentials) || {};
    clear(list);
    const profiles = Object.keys(credentials);
    if (!profiles.length) {
      list.appendChild(el("div", "empty", "还没有 API 凭据。"));
    }
    profiles.forEach((profile) => {
      const raw = credentials[profile] || {};
      list.appendChild(card(
        profile,
        `${raw.env || "?"} · ${raw.present ? "已配置" : "缺失"}`,
        [
          ["编辑", () => editCredential(profile)],
          ["删除", () => deleteCredential(profile), true],
        ]));
    });
  }

  function renderSystemInfo() {
    const box = $("system-info");
    clear(box);
    const system = data.system || {};
    if (system.error) {
      box.appendChild(el("div", "empty", system.error));
      return;
    }
    const paths = system.paths || {};
    const rows = [
      ["配置文件", paths.config || "—"],
      ["LLM 注册表", paths.llm || "—"],
      ["Secret 文件", paths.env || "—"],
      ["服务商", String(Object.keys(system.providers || {}).length)],
      ["模型", String(Object.keys(system.models || {}).length)],
      ["凭据", String(Object.keys(system.credentials || {}).length)],
      ["待重启", system.restart_required ? "是" : "否"],
    ];
    rows.forEach(([key, value]) => {
      const row = el("div", "info-row");
      row.appendChild(el("span", "info-key", key));
      row.appendChild(el("span", "info-value", value));
      box.appendChild(row);
    });
  }

  function renderRuntime() {
    const regular = data.regular || {};
    const agents = data.agents || [];
    const front = $("front-select");
    clear(front);
    agents.forEach((agent) => option(
      front, agent.agent_id, agent.agent_id,
      agent.agent_id === regular.front_agent));
    $("max-teams").value = String(regular.max_parallel_teams || 3);
    $("max-solo-turns").value = String(regular.max_solo_turns || 24);
    $("prompt-log-enabled").checked = !!regular.prompt_log_enabled;
  }

  function renderAgents() {
    const box = $("agent-settings-list");
    clear(box);
    const regular = data.regular || {};
    const piAgents = new Set(regular.pi_work_agents || []);
    (data.agents || []).forEach((agent) => {
      const row = el("div", "agent-setting-row");
      const top = el("div", "agent-setting-main");
      const enabled = el("input");
      enabled.type = "checkbox";
      enabled.checked = !!agent.enabled;
      enabled.disabled = !!agent.is_front;
      enabled.dataset.agentEnabled = agent.agent_id;
      const name = el("span", "agent-setting-id", agent.agent_id);
      top.appendChild(enabled);
      top.appendChild(name);
      if (agent.is_front) top.appendChild(el("span", "badge", "FRONT"));
      top.appendChild(el("span", "muted", agent.status || ""));

      const engine = el("select");
      engine.dataset.agentEngine = agent.agent_id;
      option(engine, "solo", "Solo", !piAgents.has(agent.agent_id));
      option(engine, "pi", "Pi", piAgents.has(agent.agent_id));
      top.appendChild(engine);

      const facts = el("div", "agent-setting-facts");
      facts.textContent =
        `model ${agent.model || "—"} · credential ${agent.credential || "—"}`;
      row.appendChild(top);
      row.appendChild(facts);
      box.appendChild(row);
    });
  }

  function renderRoles() {
    const list = $("role-list");
    clear(list);
    const roles = (data.roles && data.roles.roles) || [];
    roles.forEach((role) => {
      const actions = [["编辑", () => editRole(role.role_id)]];
      if (role.builtin) {
        actions.push(["恢复默认", () => resetRole(role.role_id), true]);
      }
      if (!role.archived && role.role_id !== "generalist") {
        actions.push(["归档", () => archiveRole(role.role_id), true]);
      }
      list.appendChild(card(
        `${role.name || role.role_id} (${role.role_id})`,
        `${role.archived ? "已归档 · " : ""}${
          role.builtin ? "内置" : "自定义"} · ${role.description || ""}`,
        actions));
    });
    if (!roles.length) list.appendChild(el("div", "empty", "还没有角色。"));
  }

  // ------------------------------------------------------------
  // provider form
  // ------------------------------------------------------------
  function editProvider(name) {
    const raw = (data.system.providers || {})[name] || {};
    editing.provider = name;
    $("provider-name").value = name;
    $("provider-name").disabled = true;
    $("provider-adapter").value = raw.adapter || "";
    $("provider-base-url").value = raw.base_url || "";
    $("provider-timeout").value = String(raw.timeout_seconds || 300);
    const defaults = $("provider-default-model");
    clear(defaults);
    option(defaults, "", "(无)");
    Object.entries(data.system.models || {}).forEach(([profile, model]) => {
      if ((model || {}).provider !== name) return;
      option(defaults, profile, profile,
        profile === (raw.default_model || ""));
    });
    $("provider-request").value = raw.request
      ? JSON.stringify(raw.request, null, 2) : "";
  }

  function newProvider() {
    editing.provider = "";
    $("provider-name").disabled = false;
    $("provider-name").value = "";
    $("provider-adapter").selectedIndex = 0;
    $("provider-base-url").value = "";
    $("provider-timeout").value = "300";
    clear($("provider-default-model"));
    option($("provider-default-model"), "", "(无)");
    Object.keys((data.system.models || {})).forEach((profile) => {
      option($("provider-default-model"), profile, profile);
    });
    $("provider-request").value = "";
  }

  async function saveProvider() {
    const payload = {
      name: $("provider-name").value.trim(),
      adapter: $("provider-adapter").value,
      base_url: $("provider-base-url").value.trim(),
      timeout_seconds: Number($("provider-timeout").value || 300),
      default_model: $("provider-default-model").value,
      request: $("provider-request").value.trim(),
    };
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({ action: "provider.upsert", data: payload }),
      });
      await load();
      toast("服务商已保存；重启后生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function deleteProvider(name) {
    if (!confirm(`删除服务商 ${name}？`)) return;
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({
          action: "provider.delete", data: { name },
        }),
      });
      await load();
      toast("服务商已删除；重启后生效");
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // model form
  // ------------------------------------------------------------
  function editModel(profile) {
    const raw = (data.system.models || {})[profile] || {};
    editing.model = profile;
    $("model-profile").value = profile;
    $("model-profile").disabled = true;
    $("model-provider").value = raw.provider || "";
    $("model-id").value = raw.model_id || "";
    $("model-context").value = String(raw.context_window || 128000);
    $("model-output").value = String(raw.max_output_tokens || 16384);
    $("model-reasoning").checked = !!raw.supports_reasoning;
  }

  function newModel() {
    editing.model = "";
    $("model-profile").disabled = false;
    $("model-profile").value = "";
    $("model-provider").selectedIndex = 0;
    $("model-id").value = "";
    $("model-context").value = "128000";
    $("model-output").value = "16384";
    $("model-reasoning").checked = true;
  }

  async function saveModel() {
    const payload = {
      profile: $("model-profile").value.trim(),
      provider: $("model-provider").value,
      model_id: $("model-id").value.trim(),
      context_window: Number($("model-context").value || 128000),
      max_output_tokens: Number($("model-output").value || 16384),
      supports_reasoning: $("model-reasoning").checked,
    };
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({ action: "model.upsert", data: payload }),
      });
      await load();
      toast("模型已保存；重启后生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function deleteModel(profile) {
    if (!confirm(`删除模型 ${profile}？`)) return;
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({
          action: "model.delete", data: { profile },
        }),
      });
      await load();
      toast("模型已删除；重启后生效");
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // credential form
  // ------------------------------------------------------------
  function editCredential(profile) {
    const raw = (data.system.credentials || {})[profile] || {};
    editing.credential = profile;
    $("credential-profile").value = profile;
    $("credential-profile").disabled = true;
    $("credential-env").value = raw.env || "";
    $("credential-key").value = "";
  }

  function newCredential() {
    editing.credential = "";
    $("credential-profile").disabled = false;
    $("credential-profile").value = "";
    $("credential-env").value = "";
    $("credential-key").value = "";
  }

  async function saveCredential() {
    const payload = {
      profile: $("credential-profile").value.trim(),
      env: $("credential-env").value.trim(),
      api_key: $("credential-key").value,
    };
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({
          action: "credential.upsert", data: payload,
        }),
      });
      await load();
      toast("凭据已保存；重启后生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function deleteCredential(profile) {
    if (!confirm(`删除凭据 ${profile}？`)) return;
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({
          action: "credential.delete", data: { profile },
        }),
      });
      await load();
      toast("凭据已删除；重启后生效");
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // regular runtime / agents
  // ------------------------------------------------------------
  async function saveRuntime() {
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          front_agent: $("front-select").value,
          max_parallel_teams: Number($("max-teams").value || 3),
          max_solo_turns: Number($("max-solo-turns").value || 24),
          prompt_log_enabled: $("prompt-log-enabled").checked,
        }),
      });
      await load();
      toast("运行设置已保存");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function saveAgents() {
    const pi = [];
    const disabled = [];
    document.querySelectorAll("[data-agent-engine]").forEach((select) => {
      if (select.value === "pi") pi.push(select.dataset.agentEngine);
    });
    document.querySelectorAll("[data-agent-enabled]").forEach((checkbox) => {
      if (!checkbox.checked) disabled.push(checkbox.dataset.agentEnabled);
    });
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          pi_work_agents: pi,
          disabled_agents: disabled,
        }),
      });
      await load();
      toast("Agent 设置已保存；引擎变更对新任务生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // roles
  // ------------------------------------------------------------
  function editRole(roleId) {
    const role = ((data.roles && data.roles.roles) || [])
      .find((item) => item.role_id === roleId);
    if (!role) return;
    editing.role = roleId;
    $("role-id").value = role.role_id;
    $("role-id").disabled = true;
    $("role-name").value = role.name || "";
    $("role-description").value = role.description || "";
    $("role-prompt").value = role.system_prompt || "";
    $("role-summary").value = (role.summary_sections || []).join("\n");
    $("role-preview").hidden = true;
  }

  function newRole() {
    editing.role = "";
    $("role-id").disabled = false;
    $("role-id").value = "";
    $("role-name").value = "";
    $("role-description").value = "";
    $("role-prompt").value = "";
    $("role-summary").value = "";
    $("role-preview").hidden = true;
  }

  async function saveRole() {
    const payload = {
      role_id: $("role-id").value.trim(),
      name: $("role-name").value.trim(),
      description: $("role-description").value.trim(),
      system_prompt: $("role-prompt").value.trim(),
      summary_sections: $("role-summary").value.split("\n")
        .map((item) => item.trim()).filter(Boolean),
    };
    try {
      await api("/api/settings/roles", {
        method: "POST",
        body: JSON.stringify({ action: "upsert", data: payload }),
      });
      await load();
      toast("角色已保存；下一个任务生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function archiveRole(roleId) {
    if (!confirm(`归档角色 ${roleId}？历史任务仍可恢复。`)) return;
    try {
      await api("/api/settings/roles", {
        method: "POST",
        body: JSON.stringify({ action: "archive", data: { role_id: roleId } }),
      });
      await load();
      toast("角色已归档");
    } catch (error) {
      toast(`归档失败：${error.message}`, true);
    }
  }

  async function resetRole(roleId) {
    if (!confirm(`把角色 ${roleId} 恢复为内置默认？`)) return;
    try {
      await api("/api/settings/roles", {
        method: "POST",
        body: JSON.stringify({ action: "reset", data: { role_id: roleId } }),
      });
      await load();
      toast("角色已恢复默认");
    } catch (error) {
      toast(`恢复失败：${error.message}`, true);
    }
  }

  function previewRole() {
    const id = $("role-id").value.trim() || "(new)";
    const name = $("role-name").value.trim() || "(未命名)";
    const prompt = $("role-prompt").value.trim();
    const summary = $("role-summary").value.split("\n")
      .map((item) => item.trim()).filter(Boolean);
    const lines = [
      "========== WORKER ROLE ==========",
      `role_id: ${id}`,
      `role_name: ${name}`,
      prompt,
    ];
    if (summary.length) {
      lines.push("Role-specific summary additions:");
      summary.forEach((item) => lines.push(`- ${item}`));
    }
    const box = $("role-preview");
    box.textContent = lines.join("\n");
    box.hidden = false;
  }

  // events
  $("provider-new").addEventListener("click", newProvider);
  $("provider-save").addEventListener("click", saveProvider);
  $("model-new").addEventListener("click", newModel);
  $("model-save").addEventListener("click", saveModel);
  $("credential-new").addEventListener("click", newCredential);
  $("credential-save").addEventListener("click", saveCredential);
  $("runtime-save").addEventListener("click", saveRuntime);
  $("agents-save").addEventListener("click", saveAgents);
  $("role-new").addEventListener("click", newRole);
  $("role-save").addEventListener("click", saveRole);
  $("role-preview-btn").addEventListener("click", previewRole);
})();
