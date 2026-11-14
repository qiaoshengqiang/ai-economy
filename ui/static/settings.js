/* AIEOS 设置面板 —— 系统设置 / 常规设置 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const modal = $("settings-modal");
  const nav = $("settings-nav");
  if (!modal || !nav) return;

  let data = null;
  let currentPane = "system-providers";
  let editing = { provider: "", model: "", credential: "", role: "" };
  let agentDraft = null;
  let agentInitial = null;
  let selectedAgent = "";
  let runtimeInitial = null;
  let providerInitial = null;
  let modelInitial = null;
  let credentialInitial = null;
  let roleInitial = null;

  const clone = (obj) => (
    typeof structuredClone === "function"
      ? structuredClone(obj)
      : JSON.parse(JSON.stringify(obj))
  );

  const PANE_CONFIG = {
    "system-providers": { save: "保存服务商", note: "修改后需重启 UI 进程" },
    "system-models": { save: "保存模型", note: "修改后需重启 UI 进程" },
    "system-credentials": { save: "保存凭据", note: "密钥只写入 .env" },
    "system-info": { readonly: true },
    "regular-runtime": { save: "保存运行设置", reset: true, kind: "run" },
    "regular-agent": { save: "保存 Agent 设置", reset: true, kind: "agent" },
    "regular-roles": { save: "保存角色", note: "对新任务生效" },
  };

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

  function checkedValue(name) {
    const input = document.querySelector(`input[name="${name}"]:checked`);
    return input ? input.value : "";
  }

  function setRadio(name, value) {
    const input = document.querySelector(
      `input[name="${name}"][value="${value}"]`);
    if (input) input.checked = true;
  }

  function flatten(obj, prefix = "", out = {}) {
    Object.entries(obj || {}).forEach(([key, value]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (value && typeof value === "object") {
        flatten(value, path, out);
      } else {
        out[path] = value;
      }
    });
    return out;
  }

  function changedCount(a, b) {
    const left = flatten(a);
    const right = flatten(b);
    return Object.keys(left).filter(
      (key) => String(left[key]) !== String(right[key])).length;
  }

  function providerSnapshot() {
    return {
      name: $("provider-name").value.trim(),
      adapter: $("provider-adapter").value,
      base_url: $("provider-base-url").value.trim(),
      timeout_seconds: Number($("provider-timeout").value || 300),
      default_model: $("provider-default-model").value,
      request: $("provider-request").value.trim(),
    };
  }

  function modelSnapshot() {
    return {
      profile: $("model-profile").value.trim(),
      provider: $("model-provider").value,
      model_id: $("model-id").value.trim(),
      context_window: Number($("model-context").value || 600000),
      max_output_tokens: Number($("model-output").value || 16384),
      supports_reasoning: $("model-reasoning").checked,
      thinking_levels: [...document.querySelectorAll(
        "#model-thinking-levels input:checked")].map((box) => box.value),
    };
  }

  function credentialSnapshot() {
    return {
      profile: $("credential-profile").value.trim(),
      env: $("credential-env").value.trim(),
      api_key: $("credential-key").value,
    };
  }

  function roleSnapshot() {
    return {
      role_id: $("role-id").value.trim(),
      name: $("role-name").value.trim(),
      description: $("role-description").value.trim(),
      system_prompt: $("role-prompt").value.trim(),
      prototype: $("role-prototype").value,
      engineering: $("role-engineering").value,
      evolution: $("role-evolution").value,
      summary: $("role-summary").value,
    };
  }

  function showPane(pane) {
    currentPane = pane;
    nav.querySelectorAll("[data-pane]").forEach((button) => {
      button.classList.toggle("active", button.dataset.pane === pane);
    });
    document.querySelectorAll(".settings-pane").forEach((section) => {
      section.hidden = section.dataset.pane !== pane;
    });
    updateFooter();
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
    updateFooter();
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
    updateFooter();
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

  // ------------------------------------------------------------
  // 运行
  // ------------------------------------------------------------
  function runtimeSnapshot() {
    const enabled = $("commit-output-enabled").checked;
    return {
      maxTeams: Number($("max-teams").value || 3),
      maxSoloTurns: Number($("max-solo-turns").value || 24),
      commitEnabled: enabled,
      commitOutputRepo: enabled
        ? $("commit-output-repo").value.trim() : "",
      commitMode: checkedValue("commit-mode") || "manual",
      commitBaseRef: $("commit-base-ref").value.trim(),
      commitAutoMerge: enabled && $("commit-auto-merge").checked,
      piTimeoutEnabled: $("pi-timeout-enabled").checked,
      piTimeoutSeconds: Number($("pi-timeout-seconds").value || 7200),
      promptLogEnabled: $("prompt-log-enabled").checked,
    };
  }

  function humanDuration(seconds) {
    const s = Math.max(0, Number(seconds) || 0);
    if (s < 60) return `${s} 秒`;
    if (s < 3600) return `≈ ${Math.round(s / 60)} 分钟`;
    const hours = Math.floor(s / 3600);
    const minutes = Math.round((s % 3600) / 60);
    return minutes ? `≈ ${hours} 小时 ${minutes} 分钟` : `≈ ${hours} 小时`;
  }

  function updateRunSummary() {
    const state = runtimeSnapshot();
    const commitLabel = {
      manual: "人类授权", limited: "有限自主", auto: "完全自主",
    }[state.commitMode] || "—";
    $("run-summary").innerHTML = [
      ["并行", state.maxTeams],
      ["单任务", `${state.maxSoloTurns} 轮`],
      ["Work 时长", state.piTimeoutEnabled
        ? humanDuration(state.piTimeoutSeconds) : "不限"],
      ["提交", state.commitEnabled ? commitLabel : "未启用"],
    ].map(([key, value]) => `<span><b>${key}</b>${value}</span>`).join("");
  }

  function updateRuntimeDependencies() {
    const enabled = $("commit-output-enabled").checked;
    $("commit-output-repo").disabled = !enabled;
    document.querySelectorAll("#commit-mode input").forEach((input) => {
      input.disabled = !enabled;
      input.closest(".mode-card").classList.toggle("disabled", !enabled);
    });
    $("commit-base-ref").disabled = !enabled;

    const mode = checkedValue("commit-mode");
    const autoAllowed = enabled && mode === "auto";
    $("commit-auto-merge").disabled = !autoAllowed;
    $("commit-auto-merge-row").classList.toggle("disabled", !autoAllowed);
    if (!autoAllowed) $("commit-auto-merge").checked = false;
    $("commit-auto-merge-hint").textContent = !enabled
      ? "启用输出仓库后才能配置自动合并。"
      : (mode === "auto"
        ? "「完全自主」允许在可快进时自动合并到基线。"
        : "当前模式不会自动合并。");

    $("pi-timeout-seconds").disabled = !$("pi-timeout-enabled").checked;
    $("timeout-human").textContent = $("pi-timeout-enabled").checked
      ? humanDuration($("pi-timeout-seconds").value)
      : "已关闭：不设总时限";
    updateRunSummary();
  }

  function applyRuntime(state) {
    $("max-teams").value = String(state.maxTeams);
    $("max-solo-turns").value = String(state.maxSoloTurns);
    $("commit-output-enabled").checked = !!state.commitEnabled;
    $("commit-output-repo").value = state.commitOutputRepo || "";
    setRadio("commit-mode", state.commitMode || "manual");
    $("commit-base-ref").value = state.commitBaseRef || "main";
    $("commit-auto-merge").checked = !!state.commitAutoMerge;
    $("pi-timeout-enabled").checked = state.piTimeoutEnabled !== false;
    $("pi-timeout-seconds").value = String(state.piTimeoutSeconds || 7200);
    $("prompt-log-enabled").checked = !!state.promptLogEnabled;
    updateRuntimeDependencies();
    updateFooter();
  }

  function renderRuntime() {
    const regular = data.regular || {};
    $("max-teams").value = String(regular.max_parallel_teams || 3);
    $("max-solo-turns").value = String(regular.max_solo_turns || 24);
    $("commit-output-enabled").checked = !!String(
      regular.commit_output_repo || "").trim();
    $("commit-output-repo").value = regular.commit_output_repo || "";
    setRadio("commit-mode", regular.commit_mode || "manual");
    $("commit-base-ref").value = regular.commit_base_ref || "main";
    $("commit-auto-merge").checked = !!regular.commit_auto_merge;
    $("pi-timeout-enabled").checked = regular.pi_timeout_enabled !== false;
    $("pi-timeout-seconds").value = String(
      regular.pi_timeout_seconds || 7200);
    $("prompt-log-enabled").checked = !!regular.prompt_log_enabled;
    updateRuntimeDependencies();
    runtimeInitial = clone(runtimeSnapshot());
    updateFooter();
  }

  async function saveRuntime() {
    const state = runtimeSnapshot();
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          max_parallel_teams: state.maxTeams,
          max_solo_turns: state.maxSoloTurns,
          commit_mode: state.commitMode,
          commit_output_repo: state.commitOutputRepo,
          commit_base_ref: state.commitBaseRef,
          commit_auto_merge: state.commitAutoMerge,
          pi_timeout_enabled: state.piTimeoutEnabled,
          pi_timeout_seconds: state.piTimeoutSeconds,
          prompt_log_enabled: state.promptLogEnabled,
        }),
      });
      await load();
      window.dispatchEvent(new CustomEvent("aieos:settings-saved"));
      toast("运行设置已保存");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // Agent（主从编辑）
  // ------------------------------------------------------------
  function buildAgentState() {
    const regular = data.regular || {};
    const piAgents = new Set(regular.pi_work_agents || []);
    const agents = {};
    (data.agents || []).forEach((agent) => {
      agents[agent.agent_id] = {
        enabled: !!agent.enabled,
        engine: piAgents.has(agent.agent_id) ? "pi" : "solo",
        status: agent.status || "",
        model: agent.model || "—",
        credential: agent.credential || "—",
      };
    });
    const ids = Object.keys(agents);
    const frontAgent = agents[regular.front_agent]
      ? regular.front_agent : (ids[0] || "");
    return {
      frontAgent,
      frontMode: regular.front_mode || "autonomous",
      workMode: regular.work_mode || "autonomous",
      agents,
    };
  }

  function renderAgentGlobal() {
    const select = $("front-select");
    clear(select);
    Object.keys(agentDraft.agents).forEach((agentId) => option(
      select, agentId, agentId, agentId === agentDraft.frontAgent));
    setRadio("front-mode", agentDraft.frontMode);
    setRadio("work-mode", agentDraft.workMode);
  }

  function renderAgentList() {
    const box = $("agent-settings-list");
    clear(box);
    Object.keys(agentDraft.agents).forEach((agentId) => {
      const agent = agentDraft.agents[agentId];
      const isFront = agentDraft.frontAgent === agentId;
      const button = el(
        "button",
        [
          "agent-list-item",
          agentId === selectedAgent ? "active" : "",
          agent.enabled ? "" : "disabled",
        ].filter(Boolean).join(" "),
      );
      button.type = "button";
      button.dataset.agent = agentId;

      button.appendChild(el(
        "span", "agent-avatar", agentId.slice(-1).toUpperCase()));
      const copy = el("span", "agent-list-copy");
      const title = el("b", "", agentId);
      if (isFront) title.appendChild(el("em", "", "FRONT"));
      copy.appendChild(title);
      copy.appendChild(el(
        "small", "", agent.enabled ? agent.status : "已停用"));
      button.appendChild(copy);
      button.appendChild(el(
        "span", "agent-list-engine", agent.engine === "pi" ? "Pi" : "Solo"));
      box.appendChild(button);
    });
  }

  function renderAgentDetail() {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) return;
    const isFront = agentDraft.frontAgent === selectedAgent;
    $("agent-detail-id").textContent = selectedAgent;
    $("agent-detail-status").textContent = agent.enabled
      ? agent.status : "已停用";
    $("agent-detail-role").textContent = isFront ? "FRONT" : "WORKER";
    $("agent-detail-role-text").textContent = isFront
      ? "Front Agent（由上方 Front Agent 决定）" : "Worker";
    $("agent-detail-model").textContent = agent.model;
    $("agent-detail-credential").textContent = agent.credential;
    $("agent-setting-enabled").checked = agent.enabled;
    $("agent-setting-enabled").disabled = isFront;
    $("agent-setting-enabled-note").textContent = isFront
      ? "Front Agent 不能被停用；如需停用，先把 Front 切到别的 Agent。"
      : "停用后不再接新任务；运行中的任务不受影响。";
    document.querySelectorAll("#agent-engine input").forEach((input) => {
      input.checked = input.value === agent.engine;
    });
  }

  function updateAgentSummary() {
    const frontLabel = agentDraft.frontMode === "autonomous"
      ? "自主" : "只传达";
    const workLabel = agentDraft.workMode === "autonomous"
      ? "自主" : "工作流";
    const piCount = Object.values(agentDraft.agents)
      .filter((agent) => agent.engine === "pi").length;
    $("agent-summary").innerHTML = [
      ["Front", agentDraft.frontAgent],
      ["Front 模式", frontLabel],
      ["Work 模式", workLabel],
      ["Pi Agent", `${piCount} 个`],
    ].map(([key, value]) => `<span><b>${key}</b>${value}</span>`).join("");
  }

  function renderAgents() {
    agentDraft = buildAgentState();
    selectedAgent = agentDraft.frontAgent;
    agentInitial = clone(agentDraft);
    renderAgentGlobal();
    renderAgentList();
    renderAgentDetail();
    updateAgentSummary();
  }

  function selectAgent(agentId) {
    if (!agentDraft.agents[agentId]) return;
    selectedAgent = agentId;
    renderAgentList();
    renderAgentDetail();
  }

  function refreshAgentViews() {
    renderAgentList();
    renderAgentDetail();
    updateAgentSummary();
    updateFooter();
  }

  async function saveAgents() {
    const pi = [];
    const disabled = [];
    Object.entries(agentDraft.agents).forEach(([agentId, agent]) => {
      if (agent.engine === "pi") pi.push(agentId);
      if (!agent.enabled) disabled.push(agentId);
    });
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          front_agent: agentDraft.frontAgent,
          front_mode: agentDraft.frontMode,
          work_mode: agentDraft.workMode,
          pi_work_agents: pi,
          disabled_agents: disabled,
        }),
      });
      await load();
      toast("Agent 设置已保存；协作与引擎变更对新任务生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  // ------------------------------------------------------------
  // 页脚 / 脏状态
  // ------------------------------------------------------------
  function updateFooter() {
    const config = PANE_CONFIG[currentPane] || {};
    const state = $("settings-footer-state");
    const actions = $("settings-footer-actions");
    const save = $("settings-save");
    const reset = $("settings-reset");

    if (config.readonly) {
      state.textContent = "只读";
      actions.hidden = true;
      return;
    }
    actions.hidden = false;
    reset.hidden = !config.reset;
    save.textContent = config.save || "保存";

    if (config.kind === "agent" && agentDraft && agentInitial) {
      const changed = changedCount(agentDraft, agentInitial);
      state.innerHTML = changed
        ? `已修改 <b>${changed}</b> 项` : "没有未保存的修改";
      save.disabled = !changed;
    } else if (config.kind === "run" && runtimeInitial) {
      const changed = changedCount(runtimeSnapshot(), runtimeInitial);
      state.innerHTML = changed
        ? `已修改 <b>${changed}</b> 项` : "没有未保存的修改";
      save.disabled = !changed;
    } else {
      const panel = {
        "system-providers": [providerSnapshot, providerInitial],
        "system-models": [modelSnapshot, modelInitial],
        "system-credentials": [credentialSnapshot, credentialInitial],
        "regular-roles": [roleSnapshot, roleInitial],
      }[currentPane];
      if (panel && panel[1]) {
        const changed = changedCount(panel[0](), panel[1]);
        state.innerHTML = changed
          ? `已修改 <b>${changed}</b> 项 · ${config.note || "保存后生效"}`
          : "没有未保存的修改";
        save.disabled = !changed;
      } else {
        state.textContent = config.note || "保存后生效";
        save.disabled = false;
      }
    }
  }

  async function saveCurrentPane() {
    if (currentPane === "system-providers") return saveProvider();
    if (currentPane === "system-models") return saveModel();
    if (currentPane === "system-credentials") return saveCredential();
    if (currentPane === "regular-runtime") return saveRuntime();
    if (currentPane === "regular-agent") return saveAgents();
    if (currentPane === "regular-roles") return saveRole();
    return undefined;
  }

  async function resetCurrentPane() {
    if (currentPane === "regular-runtime") {
      applyRuntime({
        maxTeams: 3,
        maxSoloTurns: 24,
        commitEnabled: false,
        commitOutputRepo: "",
        commitMode: "manual",
        commitBaseRef: "main",
        commitAutoMerge: false,
        piTimeoutEnabled: true,
        piTimeoutSeconds: 7200,
        promptLogEnabled: false,
      });
      toast("已恢复运行默认值（尚未保存）");
      return;
    }
    if (currentPane === "regular-agent") {
      const ids = Object.keys(agentDraft.agents);
      const next = clone(agentDraft);
      next.frontAgent = ids[0] || "";
      next.frontMode = "autonomous";
      next.workMode = "autonomous";
      ids.forEach((agentId, index) => {
        next.agents[agentId].enabled = true;
        next.agents[agentId].engine = index === 0 ? "solo" : "pi";
      });
      agentDraft = next;
      selectedAgent = next.frontAgent;
      renderAgentGlobal();
      renderAgentList();
      renderAgentDetail();
      updateAgentSummary();
      updateFooter();
      toast("已恢复 Agent 默认值（尚未保存）");
    }
  }

  async function cancelCurrentPane() {
    if (currentPane === "regular-agent") {
      agentDraft = clone(agentInitial);
      selectedAgent = agentDraft.frontAgent;
      renderAgentGlobal();
      renderAgentList();
      renderAgentDetail();
      updateAgentSummary();
      updateFooter();
      return;
    }
    if (currentPane === "regular-runtime") {
      applyRuntime(runtimeInitial);
      return;
    }
    await load();
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
    providerInitial = clone(providerSnapshot());
    updateFooter();
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
    providerInitial = clone(providerSnapshot());
    updateFooter();
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
    const ok = await askConfirm(
      "删除服务商？",
      "删除后引用该服务商的模型需要先调整，否则配置校验会失败。",
      name, "danger", "删除服务商");
    if (!ok) return;
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
    $("model-context").value = String(raw.context_window || 600000);
    $("model-output").value = String(raw.max_output_tokens || 16384);
    $("model-reasoning").checked = !!raw.supports_reasoning;
    const levels = new Set(raw.thinking_levels || []);
    document.querySelectorAll("#model-thinking-levels input").forEach((box) => {
      box.checked = levels.has(box.value);
    });
    modelInitial = clone(modelSnapshot());
    updateFooter();
  }

  function newModel() {
    editing.model = "";
    $("model-profile").disabled = false;
    $("model-profile").value = "";
    $("model-provider").selectedIndex = 0;
    $("model-id").value = "";
    $("model-context").value = "600000";
    $("model-output").value = "16384";
    $("model-reasoning").checked = true;
    document.querySelectorAll("#model-thinking-levels input").forEach((box) => {
      box.checked = box.value === "off";
    });
    modelInitial = clone(modelSnapshot());
    updateFooter();
  }

  async function saveModel() {
    const payload = {
      profile: $("model-profile").value.trim(),
      provider: $("model-provider").value,
      model_id: $("model-id").value.trim(),
      context_window: Number($("model-context").value || 600000),
      max_output_tokens: Number($("model-output").value || 16384),
      supports_reasoning: $("model-reasoning").checked,
      thinking_levels: [...document.querySelectorAll(
        "#model-thinking-levels input:checked")].map((box) => box.value),
    };
    if (!payload.thinking_levels.length) payload.thinking_levels = ["off"];
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
    const ok = await askConfirm(
      "删除模型？",
      "删除后引用该 profile 的 Agent 需要先调整，否则配置校验会失败。",
      profile, "danger", "删除模型");
    if (!ok) return;
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
    credentialInitial = clone(credentialSnapshot());
    updateFooter();
  }

  function newCredential() {
    editing.credential = "";
    $("credential-profile").disabled = false;
    $("credential-profile").value = "";
    $("credential-env").value = "";
    $("credential-key").value = "";
    credentialInitial = clone(credentialSnapshot());
    updateFooter();
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
    const ok = await askConfirm(
      "删除 API 凭据？",
      "删除后引用该 profile 的 Agent 需要先调整，否则配置校验会失败。",
      profile, "danger", "删除凭据");
    if (!ok) return;
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
  // roles
  // ------------------------------------------------------------
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
    const phase = role.phase_requirements || {};
    $("role-prototype").value = (phase.prototype || []).join("\n");
    $("role-engineering").value = (phase.engineering || []).join("\n");
    $("role-evolution").value = (phase.evolution || []).join("\n");
    $("role-summary").value = (role.summary_sections || []).join("\n");
    $("role-preview").hidden = true;
    roleInitial = clone(roleSnapshot());
    updateFooter();
  }

  function newRole() {
    editing.role = "";
    $("role-id").disabled = false;
    $("role-id").value = "";
    $("role-name").value = "";
    $("role-description").value = "";
    $("role-prompt").value = "";
    $("role-prototype").value = "";
    $("role-engineering").value = "";
    $("role-evolution").value = "";
    $("role-summary").value = "";
    $("role-preview").hidden = true;
    roleInitial = clone(roleSnapshot());
    updateFooter();
  }

  async function saveRole() {
    const payload = {
      role_id: $("role-id").value.trim(),
      name: $("role-name").value.trim(),
      description: $("role-description").value.trim(),
      system_prompt: $("role-prompt").value.trim(),
      phase_requirements: {
        prototype: $("role-prototype").value.split("\n")
          .map((item) => item.trim()).filter(Boolean),
        engineering: $("role-engineering").value.split("\n")
          .map((item) => item.trim()).filter(Boolean),
        evolution: $("role-evolution").value.split("\n")
          .map((item) => item.trim()).filter(Boolean),
      },
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
    const ok = await askConfirm(
      "归档角色？", "历史任务仍可恢复；新任务将不再使用这个角色。",
      roleId, "warning", "归档角色");
    if (!ok) return;
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
    const ok = await askConfirm(
      "恢复默认角色？", "当前自定义内容会被内置默认值覆盖。",
      roleId, "warning", "恢复默认");
    if (!ok) return;
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
      "========== Worker 角色 ==========",
      `role_id: ${id}`,
      `role_name: ${name}`,
    ];
    if (prompt) lines.push("通用专业要求：", prompt);
    ["prototype", "engineering", "evolution"].forEach((phase) => {
      const items = $(`role-${phase}`).value.split("\n")
        .map((item) => item.trim()).filter(Boolean);
      if (!items.length) return;
      lines.push(`${phase} 阶段：`);
      items.forEach((item) => lines.push(`- ${item}`));
    });
    if (summary.length) {
      lines.push("角色额外总结项：");
      summary.forEach((item) => lines.push(`- ${item}`));
    }
    const box = $("role-preview");
    box.textContent = lines.join("\n");
    box.hidden = false;
  }

  async function askConfirm(title, message, target, tone, confirmText) {
    if (typeof window.aieosConfirm === "function") {
      return window.aieosConfirm({
        tone,
        title,
        message,
        target,
        confirmText,
        cancelText: "取消",
      });
    }
    return window.confirm(message);
  }

  // ------------------------------------------------------------
  // events
  // ------------------------------------------------------------
  $("settings-btn").addEventListener("click", open);
  $("settings-close").addEventListener("click", close);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) close();
  });
  nav.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pane]");
    if (button) showPane(button.dataset.pane);
  });

  // 系统设置页的编辑表单：输入后实时更新页脚脏状态。
  const systemPanes = new Set([
    "system-providers", "system-models",
    "system-credentials", "regular-roles",
  ]);
  const settingsContent = document.querySelector(".settings-content");
  const refreshSystemFooter = () => {
    if (systemPanes.has(currentPane)) updateFooter();
  };
  settingsContent.addEventListener("input", refreshSystemFooter);
  settingsContent.addEventListener("change", refreshSystemFooter);

  $("provider-new").addEventListener("click", newProvider);
  $("model-new").addEventListener("click", newModel);
  $("credential-new").addEventListener("click", newCredential);
  $("role-new").addEventListener("click", newRole);
  $("role-preview-btn").addEventListener("click", previewRole);

  $("settings-save").addEventListener("click", saveCurrentPane);
  $("settings-reset").addEventListener("click", resetCurrentPane);
  $("settings-cancel").addEventListener("click", cancelCurrentPane);

  $("commit-output-enabled").addEventListener("change", () => {
    updateRuntimeDependencies();
    updateFooter();
  });
  document.querySelectorAll("#commit-mode input").forEach((input) => {
    input.addEventListener("change", () => {
      updateRuntimeDependencies();
      updateFooter();
    });
  });
  $("commit-auto-merge").addEventListener("change", updateFooter);
  $("pi-timeout-enabled").addEventListener("change", () => {
    updateRuntimeDependencies();
    updateFooter();
  });
  [
    "max-teams", "max-solo-turns", "commit-output-repo",
    "commit-base-ref", "pi-timeout-seconds", "prompt-log-enabled",
  ].forEach((id) => {
    const node = $(id);
    node.addEventListener("change", () => {
      updateRuntimeDependencies();
      updateFooter();
    });
    node.addEventListener("input", () => {
      updateRuntimeDependencies();
      updateFooter();
    });
  });

  $("front-select").addEventListener("change", () => {
    const next = $("front-select").value;
    if (!agentDraft.agents[next]) return;
    agentDraft.frontAgent = next;
    if (!agentDraft.agents[next].enabled) {
      agentDraft.agents[next].enabled = true;
    }
    selectedAgent = next;
    refreshAgentViews();
  });
  document.querySelectorAll('input[name="front-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      agentDraft.frontMode = input.value;
      updateAgentSummary();
      updateFooter();
    });
  });
  document.querySelectorAll('input[name="work-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      agentDraft.workMode = input.value;
      updateAgentSummary();
      updateFooter();
    });
  });
  $("agent-settings-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-agent]");
    if (item) selectAgent(item.dataset.agent);
  });
  $("agent-setting-enabled").addEventListener("change", () => {
    if (agentDraft.frontAgent === selectedAgent) {
      $("agent-setting-enabled").checked = true;
      return;
    }
    agentDraft.agents[selectedAgent].enabled = $("agent-setting-enabled").checked;
    refreshAgentViews();
  });
  document.querySelectorAll("#agent-engine input").forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      agentDraft.agents[selectedAgent].engine = input.value;
      refreshAgentViews();
    });
  });
})();
