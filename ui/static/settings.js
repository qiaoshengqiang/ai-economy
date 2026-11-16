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
  let showArchivedAgents = false;
  let runtimeInitial = null;
  let providerInitial = null;
  let modelInitial = null;
  let credentialInitial = null;
  let roleInitial = null;
  //: 上一次由「预设 / Adapter 默认值」自动写进 Base URL 的值。
  //: 用户手动改过 Base URL 后，切换 Adapter 就不再覆盖它。
  let providerAutoBaseUrl = "";

  const PROVIDER_PRESETS = {
    opencode: {
      name: "opencode",
      adapter: "openai_compatible",
      base_url: "https://opencode.ai/inference/go/openai/v1",
      hint: "OpenCode Go（订阅）。凭据环境变量用 OPENCODE_KEY；" +
        "模型 ID 可用 glm-5.3 / kimi-k3 / minimax-m3 / qwen3.8-max。" +
        "Go 端点要求 x-opencode-session 请求头，仓库里的 opencode " +
        "服务商已经预置。",
    },
    opencode_zen: {
      name: "opencode_zen",
      adapter: "openai_compatible",
      base_url: "https://opencode.ai/inference/openai/v1",
      hint: "OpenCode Zen（按量付费）。凭据环境变量用 OPENCODE_KEY；" +
        "需要先在 Console 充值。免费模型只允许 OpenCode 客户端调用，" +
        "外部调用会返回 403。",
    },
    deepseek: {
      name: "deepseek",
      adapter: "deepseek",
      base_url: "https://api.deepseek.com",
      hint: "DeepSeek 官方端点。模型 ID 例如 deepseek-chat / deepseek-flash。",
    },
    doubao: {
      name: "doubao",
      adapter: "doubao",
      base_url: "https://ark.cn-beijing.volces.com/api/v3",
      hint: "豆包（火山方舟）。模型 ID 通常是控制台里的推理接入点 ID（ep-...）。",
    },
    bailian: {
      name: "bailian",
      adapter: "bailian",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      hint: "阿里云百炼。必须使用 compatible-mode/v1 这个 OpenAI 兼容入口。",
    },
  };

  const clone = (obj) => (
    typeof structuredClone === "function"
      ? structuredClone(obj)
      : JSON.parse(JSON.stringify(obj))
  );

  const PANE_CONFIG = {
    "system-providers": {
      save: "保存服务商",
      note: "模型使用「保存模型」单独保存",
    },
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

  function adapterSpec(name) {
    const adapters = (data && data.system && data.system.adapters) || [];
    return adapters.find((item) => item.name === name) || null;
  }

  function adapterDefaultBaseUrl(name) {
    const spec = adapterSpec(name);
    return (spec && spec.default_base_url) || "";
  }

  function updateProviderHints() {
    const select = $("provider-adapter");
    const input = $("provider-base-url");
    const hint = $("provider-base-url-hint");
    if (!select || !input || !hint) return;
    const spec = adapterSpec(select.value);
    const current = input.value.trim().replace(/\/+$/, "");
    if (spec && !spec.requires_base_url && !spec.default_base_url) {
      input.placeholder = "https://api.example.com";
      hint.textContent = "这个 Adapter 不需要 Base URL。";
    } else if (current) {
      input.placeholder = spec && spec.default_base_url
        ? spec.default_base_url : "https://api.example.com";
      hint.textContent = `请求会发往 ${current}/chat/completions`;
    } else if (spec && spec.default_base_url) {
      input.placeholder = spec.default_base_url;
      hint.textContent = `可留空，默认使用 ${spec.default_base_url}`;
    } else {
      input.placeholder = "https://api.example.com";
      hint.textContent = "这个 Adapter 必须填写 Base URL。";
    }
    const preset = PROVIDER_PRESETS[$("provider-preset").value];
    $("provider-preset-hint").textContent = preset
      ? preset.hint
      : "选择预设会自动填 Adapter 和 Base URL；也可以完全自定义。";
  }

  function applyProviderPreset(key) {
    const preset = PROVIDER_PRESETS[key];
    if (!preset) {
      updateProviderHints();
      return;
    }
    if (!$("provider-name").disabled) {
      $("provider-name").value = preset.name;
    }
    $("provider-adapter").value = preset.adapter;
    providerAutoBaseUrl = preset.base_url;
    $("provider-base-url").value = preset.base_url;
    updateProviderHints();
    updateFooter();
  }

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
      provider: editing.provider,
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

  async function load(preferredProvider = "") {
    data = await api("/api/settings");
    renderAll();
    // 每次打开 / 保存后重新选中一个有效服务商；没有服务商时回到新建表单。
    editing = { provider: "", model: "", credential: "", role: "" };
    const providers = (data.system && data.system.providers) || {};
    const models = (data.system && data.system.models) || {};
    const names = Object.keys(providers);
    const withModels = names.filter((name) => Object.values(models).some(
      (model) => String((model || {}).provider || "") === name));
    const target = names.includes(preferredProvider)
      ? preferredProvider
      : (withModels.find((name) => name !== "mock")
        || withModels[0]
        || names.find((name) => name !== "mock")
        || names[0]
        || "");
    if (target) {
      editProvider(target);
    } else {
      newProvider();
    }
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
    const models = (data.system && data.system.models) || {};
    clear(list);
    const names = Object.keys(providers);
    $("provider-count").textContent = `${names.length} 个`;
    if (!names.length) {
      list.appendChild(el("div", "empty", "还没有服务商。"));
    }
    names.forEach((name) => {
      const raw = providers[name] || {};
      const count = Object.values(models).filter(
        (model) => String((model || {}).provider || "") === name).length;
      const item = el(
        "div",
        `settings-item provider-list-item${
          editing.provider === name ? " active" : ""}`);
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.setAttribute("aria-pressed",
        editing.provider === name ? "true" : "false");

      const body = el("div", "settings-item-body");
      body.appendChild(el("div", "settings-item-title", name));
      body.appendChild(el("div", "settings-item-meta",
        `adapter ${raw.adapter || "?"} · ${count} 个模型`));
      item.appendChild(body);

      const tools = el("div", "settings-item-actions");
      const remove = el("button", "mini-btn danger", "删除");
      remove.type = "button";
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteProvider(name);
      });
      tools.appendChild(remove);
      item.appendChild(tools);

      const choose = () => {
        if (editing.provider !== name) editProvider(name);
      };
      item.addEventListener("click", choose);
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          choose();
        }
      });
      list.appendChild(item);
    });

    const select = $("provider-adapter");
    clear(select);
    const adapters = (data.system && data.system.adapters) || [];
    adapters.forEach((item) => option(
      select, item.name,
      `${item.name}${item.requires_base_url ? "" : " (no base_url)"}`));
    updateProviderHints();
  }

  function renderModels() {
    const list = $("model-list");
    const models = (data.system && data.system.models) || {};
    const provider = editing.provider;
    const providerRaw = ((data.system && data.system.providers) || {})[
      provider] || {};
    clear(list);
    const entries = Object.entries(models).filter(
      ([, raw]) => String((raw || {}).provider || "") === provider);
    if (!provider) {
      list.appendChild(el("div", "empty", "先保存服务商，再添加模型。"));
    } else if (!entries.length) {
      list.appendChild(el("div", "empty", "这个服务商还没有模型。"));
    }
    entries.forEach(([profile, raw]) => {
      const isDefault = providerRaw.default_model === profile;
      list.appendChild(card(
        isDefault ? `${profile} · 默认` : profile,
        `${(raw || {}).model_id || "?"} · context ${
          (raw || {}).context_window || "?"} / output ${
          (raw || {}).max_output_tokens || "?"}`,
        [
          ["编辑", () => editModel(profile)],
          ["删除", () => deleteModel(profile), true],
        ]));
    });
    $("model-new").disabled = !provider;
    $("provider-model-hint").textContent = !provider
      ? "先保存服务商，再添加模型。"
      : (entries.length
        ? "模型 profile 只在当前服务商下可见。"
        : "还没有模型；添加后 Agent 才能引用这个服务商。");
    if (!provider) hideModelEditor();
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
      maxBuiltinTurns: Number($("max-builtin-turns").value || 24),
      maxReadOnlyTurns: Number($("max-read-only-turns").value || 8),
      frontMaxInspect: Number($("front-max-inspect").value || 4),
      commitEnabled: enabled,
      commitOutputRepo: enabled
        ? $("commit-output-repo").value.trim() : "",
      commitMode: checkedValue("commit-mode") || "manual",
      commitBaseRef: $("commit-base-ref").value.trim(),
      commitAutoMerge: enabled && $("commit-auto-merge").checked,
      piTimeoutEnabled: $("pi-timeout-enabled").checked,
      piTimeoutSeconds: Number($("pi-timeout-seconds").value || 7200),
      promptLogEnabled: $("prompt-log-enabled").checked,
      timezone: $("timezone").value || "Asia/Shanghai",
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
      ["内置引擎", `${state.maxBuiltinTurns} 轮`],
      ["Pi 时长", state.piTimeoutEnabled
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
    $("max-builtin-turns").value = String(state.maxBuiltinTurns);
    $("max-read-only-turns").value = String(state.maxReadOnlyTurns);
    $("front-max-inspect").value = String(state.frontMaxInspect);
    $("commit-output-enabled").checked = !!state.commitEnabled;
    $("commit-output-repo").value = state.commitOutputRepo || "";
    setRadio("commit-mode", state.commitMode || "manual");
    $("commit-base-ref").value = state.commitBaseRef || "main";
    $("commit-auto-merge").checked = !!state.commitAutoMerge;
    $("pi-timeout-enabled").checked = state.piTimeoutEnabled !== false;
    $("pi-timeout-seconds").value = String(state.piTimeoutSeconds || 7200);
    $("prompt-log-enabled").checked = !!state.promptLogEnabled;
    $("timezone").value = state.timezone || "Asia/Shanghai";
    updateRuntimeDependencies();
    updateFooter();
  }

  function renderRuntime() {
    const regular = data.regular || {};
    $("max-teams").value = String(regular.max_parallel_teams || 3);
    $("max-builtin-turns").value = String(
      regular.max_builtin_turns ?? regular.max_solo_turns ?? 24);
    $("max-read-only-turns").value = String(
      regular.max_read_only_turns ?? 8);
    $("front-max-inspect").value = String(
      regular.front_max_inspect ?? 4);
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
    $("timezone").value = regular.timezone || "Asia/Shanghai";
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
          max_builtin_turns: state.maxBuiltinTurns,
          max_read_only_turns: state.maxReadOnlyTurns,
          front_max_inspect: state.frontMaxInspect,
          commit_mode: state.commitMode,
          commit_output_repo: state.commitOutputRepo,
          commit_base_ref: state.commitBaseRef,
          commit_auto_merge: state.commitAutoMerge,
          pi_timeout_enabled: state.piTimeoutEnabled,
          pi_timeout_seconds: state.piTimeoutSeconds,
          prompt_log_enabled: state.promptLogEnabled,
          timezone: state.timezone,
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
  // Agent（主从编辑 + 新增 / 归档）
  // ------------------------------------------------------------
  function systemModels() {
    return (data && data.system && data.system.models) || {};
  }

  function systemProviders() {
    return (data && data.system && data.system.providers) || {};
  }

  function systemCredentials() {
    return (data && data.system && data.system.credentials) || {};
  }

  function modelsForProvider(provider) {
    return Object.entries(systemModels())
      .filter(([, raw]) => String((raw || {}).provider || "") === provider)
      .map(([profile, raw]) => ({ profile, ...(raw || {}) }));
  }

  function agentProviderOf(agent) {
    if (!agent) return "";
    const model = systemModels()[agent.model] || {};
    return String(model.provider || agent.provider || "");
  }

  function fillProviderOptions(select, current) {
    clear(select);
    option(select, "", "(未选择)");
    const providers = systemProviders();
    Object.keys(providers).forEach((name) => {
      const count = modelsForProvider(name).length;
      option(select, name, `${name} · ${count ? `${count} 个模型` : "无模型"}`);
    });
    if (current && !Object.prototype.hasOwnProperty.call(providers, current)) {
      option(select, current, `${current} · 未知服务商`);
    }
    select.value = current && Array.from(select.options).some(
      (item) => item.value === current) ? current : "";
  }

  function fillModelOptions(select, provider, current, autoSelect = false) {
    clear(select);
    const models = modelsForProvider(provider);
    if (!models.length) {
      option(select, "", provider ? "(该服务商还没有模型)" : "(请先选择服务商)");
      select.value = "";
      return;
    }
    option(select, "", "(请选择模型)");
    models.forEach((model) => option(
      select, model.profile,
      `${model.profile} · ${model.model_id || "?"}`));
    if (models.some((model) => model.profile === current)) {
      select.value = current;
    } else {
      select.value = autoSelect ? models[0].profile : "";
    }
  }

  function fillCredentialOptions(select, current) {
    clear(select);
    option(select, "", "(请选择凭据)");
    Object.entries(systemCredentials()).forEach(([profile, raw]) => {
      const env = String((raw || {}).env || "");
      const state = raw && raw.present ? "已配置" : "缺失";
      option(select, profile, `${profile} · ${env} · ${state}`);
    });
    select.value = current && Array.from(select.options).some(
      (item) => item.value === current) ? current : "";
  }

  function buildAgentState() {
    const regular = data.regular || {};
    const piAgents = new Set(regular.pi_work_agents || []);
    const agents = {};
    (data.agents || []).forEach((agent) => {
      const model = String(agent.model || "");
      const modelRaw = systemModels()[model] || {};
      agents[agent.agent_id] = {
        name: String(agent.name || agent.agent_id),
        enabled: !!agent.enabled,
        archived: !!agent.archived,
        engine: piAgents.has(agent.agent_id) ? "pi" : "builtin",
        status: agent.status || "",
        provider: String(modelRaw.provider || ""),
        model,
        credential: String(agent.credential || ""),
      };
    });
    const ids = Object.keys(agents);
    let frontAgent = agents[regular.front_agent]
      ? regular.front_agent : (ids[0] || "");
    if (agents[frontAgent] && agents[frontAgent].archived) {
      frontAgent = ids.find((id) => !agents[id].archived) || "";
    }
    return {
      frontAgent,
      frontMode: regular.front_mode || "autonomous",
      workMode: regular.work_mode || "autonomous",
      agents,
    };
  }

  function activeAgentIds() {
    return Object.keys(agentDraft.agents)
      .filter((agentId) => !agentDraft.agents[agentId].archived);
  }

  function renderAgentGlobal() {
    const select = $("front-select");
    clear(select);
    activeAgentIds().forEach((agentId) => option(
      select, agentId, agentDraft.agents[agentId].name,
      agentId === agentDraft.frontAgent));
    setRadio("front-mode", agentDraft.frontMode);
    setRadio("work-mode", agentDraft.workMode);
  }

  function renderAgentList() {
    const box = $("agent-settings-list");
    const entries = Object.entries(agentDraft.agents)
      .filter(([, agent]) => (
        showArchivedAgents ? agent.archived : !agent.archived));
    const archivedCount = Object.values(agentDraft.agents)
      .filter((agent) => agent.archived).length;
    $("agent-archived-count").textContent = String(archivedCount);
    $("agent-archived-toggle").classList.toggle(
      "active", showArchivedAgents);
    clear(box);

    if (!entries.length) {
      box.appendChild(el(
        "div", "empty",
        showArchivedAgents ? "没有已归档的 Agent。" : "没有可用的 Agent。"));
      return;
    }

    entries.forEach(([agentId, agent]) => {
      const isFront = agentDraft.frontAgent === agentId;
      const button = el(
        "button",
        [
          "agent-list-item",
          agentId === selectedAgent ? "active" : "",
          agent.archived ? "archived" : "",
          agent.enabled || agent.archived ? "" : "disabled",
        ].filter(Boolean).join(" "),
      );
      button.type = "button";
      button.dataset.agent = agentId;

      const initial = agentId.slice(-1).toUpperCase();
      button.appendChild(el("span", "agent-avatar", initial));
      const copy = el("span", "agent-list-copy");
      const title = el("b", "", agent.name);
      if (isFront) title.appendChild(el("em", "", "FRONT"));
      if (agent.archived) title.appendChild(el("em", "archived", "ARCHIVED"));
      copy.appendChild(title);
      copy.appendChild(el(
        "small", "",
        `${agentId} · ${
          agent.archived ? "已归档"
            : (agent.enabled ? agent.status : "已停用")
        }`));
      button.appendChild(copy);
      button.appendChild(el(
        "span", "agent-list-engine",
        agent.engine === "pi" ? "Pi" : "内置"));
      box.appendChild(button);
    });
  }

  function renderAgentDetail() {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) {
      $("agent-detail-name").textContent = "—";
      $("agent-detail-id").textContent = "—";
      $("agent-detail-status").textContent = "—";
      $("agent-detail-role").textContent = "—";
      $("agent-detail-role-text").textContent = "—";
      clear($("agent-detail-provider"));
      clear($("agent-detail-model"));
      clear($("agent-detail-credential"));
      $("agent-detail-provider").disabled = true;
      $("agent-detail-model").disabled = true;
      $("agent-detail-credential").disabled = true;
      $("agent-setting-enabled").checked = false;
      $("agent-setting-enabled").disabled = true;
      $("agent-archive-btn").hidden = true;
      $("agent-restore-btn").hidden = true;
      return;
    }
    const isFront = agentDraft.frontAgent === selectedAgent;
    $("agent-detail-name").textContent = agent.name;
    $("agent-detail-id").textContent = selectedAgent;
    $("agent-detail-status").textContent = agent.archived
      ? "已归档"
      : (agent.enabled ? agent.status : "已停用");
    $("agent-detail-role").textContent = agent.archived
      ? "ARCHIVED" : (isFront ? "FRONT" : "WORKER");
    $("agent-detail-role-text").textContent = agent.archived
      ? "已归档（历史保留）"
      : (isFront ? "Front Agent（由上方 Front Agent 决定）" : "Worker");
    const provider = agentProviderOf(agent);
    fillProviderOptions($("agent-detail-provider"), provider);
    fillModelOptions($("agent-detail-model"), provider, agent.model);
    fillCredentialOptions($("agent-detail-credential"), agent.credential);
    $("agent-detail-provider").disabled = agent.archived;
    $("agent-detail-model").disabled = agent.archived;
    $("agent-detail-credential").disabled = agent.archived;
    $("agent-setting-enabled").checked = agent.enabled;
    $("agent-setting-enabled").disabled = isFront || agent.archived;
    $("agent-setting-enabled-note").textContent = agent.archived
      ? "已归档的 Agent 不会接新任务；恢复后可重新启用。"
      : (isFront
        ? "Front Agent 不能被停用；如需停用，先把 Front 切到别的 Agent。"
        : "停用后不再接新任务；运行中的任务不受影响。");
    document.querySelectorAll("#agent-engine input").forEach((input) => {
      input.checked = input.value === agent.engine;
      input.disabled = agent.archived;
    });
    $("agent-archive-btn").hidden = agent.archived;
    $("agent-archive-btn").disabled = isFront;
    $("agent-restore-btn").hidden = !agent.archived;
  }

  function updateAgentSummary() {
    const frontLabel = agentDraft.frontMode === "autonomous"
      ? "自主" : "只传达";
    const workLabel = agentDraft.workMode === "autonomous"
      ? "自主" : "工作流";
    const front = agentDraft.agents[agentDraft.frontAgent];
    const active = activeAgentIds();
    const piCount = active.filter(
      (agentId) => agentDraft.agents[agentId].engine === "pi").length;
    $("agent-summary").innerHTML = [
      ["Front", front ? front.name : "—"],
      ["Front 模式", frontLabel],
      ["Pi 工作模式", workLabel],
      ["Pi 引擎 Agent", `${piCount} 个`],
    ].map(([key, value]) => `<span><b>${key}</b>${value}</span>`).join("");
  }

  function renderAgents() {
    agentDraft = buildAgentState();
    showArchivedAgents = false;
    selectedAgent = agentDraft.frontAgent
      || Object.keys(agentDraft.agents)[0] || "";
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
    renderAgentGlobal();
    renderAgentList();
    renderAgentDetail();
    updateAgentSummary();
    updateFooter();
  }

  async function archiveSelectedAgent() {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) return;
    if (agentDraft.frontAgent === selectedAgent) {
      toast("Front Agent 不能被归档", true);
      return;
    }
    const ok = await askConfirm(
      "归档 Agent？",
      "归档会停用并隐藏该 Agent；身份、账本、任务和记忆都会保留。",
      `${agent.name} · ${selectedAgent}`,
      "warning",
      "归档 Agent",
    );
    if (!ok) return;
    agent.archived = true;
    agent.enabled = false;
    showArchivedAgents = false;
    const next = activeAgentIds()[0] || "";
    selectedAgent = next;
    refreshAgentViews();
    await saveAgents("Agent 已归档并保存");
  }

  async function restoreSelectedAgent() {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent || !agent.archived) return;
    agent.archived = false;
    agent.enabled = false;
    showArchivedAgents = false;
    refreshAgentViews();
    await saveAgents("Agent 已恢复（仍为停用）并保存");
  }

  function populateNewAgentModels(current = "") {
    fillModelOptions(
      $("new-agent-model"),
      $("new-agent-provider").value,
      current,
      true,
    );
  }

  function openAgentCreate() {
    const card = $("agent-create-card");
    card.hidden = false;
    $("new-agent-id").value = "";
    $("new-agent-name").value = "";
    $("new-agent-balance").value = "100";

    const providerSelect = $("new-agent-provider");
    clear(providerSelect);
    const allProviders = Object.keys(systemProviders()).filter(
      (name) => modelsForProvider(name).length);
    let providerNames = allProviders.filter((name) => name !== "mock");
    if (!providerNames.length) providerNames = allProviders;
    if (!providerNames.length) {
      option(providerSelect, "", "(还没有可用服务商)");
    } else {
      providerNames.forEach((name) => option(
        providerSelect, name,
        `${name} · ${modelsForProvider(name).length} 个模型`));
      const preferred = agentProviderOf(agentDraft.agents[selectedAgent]);
      providerSelect.value = providerNames.includes(preferred)
        ? preferred : providerNames[0];
    }
    populateNewAgentModels();

    fillCredentialOptions($("new-agent-credential"), "");
    const firstPresent = Object.entries(systemCredentials())
      .find(([, raw]) => raw && raw.present);
    if (firstPresent) $("new-agent-credential").value = firstPresent[0];

    $("new-agent-id").focus();
    card.scrollIntoView({ block: "nearest" });
  }

  function closeAgentCreate() {
    $("agent-create-card").hidden = true;
  }

  async function createAgent() {
    const agentId = $("new-agent-id").value.trim();
    const name = $("new-agent-name").value.trim();
    const model = $("new-agent-model").value;
    const credential = $("new-agent-credential").value;
    const balance = Number($("new-agent-balance").value || 0);
    if (!/^[a-z][a-z0-9_]{2,31}$/.test(agentId)) {
      toast("Agent ID 不合法：小写字母开头，3–32 位字母/数字/下划线", true);
      return;
    }
    if (!name || name.length > 40) {
      toast("中文名不能为空，且不超过 40 个字符", true);
      return;
    }
    if (!model || !credential) {
      toast("请选择服务商、模型和凭据", true);
      return;
    }
    if (!Number.isFinite(balance) || balance < 0 || balance > 1000) {
      toast("初始余额必须在 0–1000 AIC 之间", true);
      return;
    }
    const credentialRaw = ((data.system && data.system.credentials) || {})[
      credential];
    if (!credentialRaw || !credentialRaw.present) {
      toast("该凭据尚未配置，请先在 API 凭据里填写 Key", true);
      return;
    }
    const ok = await askConfirm(
      "创建 Agent？",
      `将写入 config/config.yaml，并在重启 UI 后创建身份、钱包与 ${balance} AIC 初始资金。`,
      `${name} · ${agentId}`,
      "warning",
      "创建 Agent",
    );
    if (!ok) return;
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({
          action: "agent.create",
          data: {
            agent_id: agentId,
            name,
            model,
            credential,
            initial_balance: balance,
          },
        }),
      });
      closeAgentCreate();
      await load();
      toast("Agent 已创建；重启 UI 后生效");
    } catch (error) {
      toast(`创建失败：${error.message}`, true);
    }
  }

  async function saveAgents(message = "") {
    const pi = [];
    const disabled = [];
    const archived = [];
    const modelChanges = [];
    const missing = [];
    Object.entries(agentDraft.agents).forEach(([agentId, agent]) => {
      if (agent.engine === "pi") pi.push(agentId);
      if (agent.archived) archived.push(agentId);
      if (!agent.enabled || agent.archived) disabled.push(agentId);
      const initial = (agentInitial.agents || {})[agentId] || {};
      const changed = (
        agent.model !== initial.model
        || agent.credential !== initial.credential
      );
      if (!changed) return;
      if (!agent.model || !agent.credential) {
        missing.push(agent.name || agentId);
        return;
      }
      modelChanges.push({
        agent_id: agentId,
        model: agent.model,
        credential: agent.credential,
      });
    });
    if (missing.length) {
      toast(`请为 ${missing.join("、")} 选择模型与凭据`, true);
      return;
    }
    const keep = selectedAgent;
    try {
      // 模型 / 凭据属于 canonical config.yaml，必须走系统设置写入口。
      for (const change of modelChanges) {
        await api("/api/settings/system", {
          method: "POST",
          body: JSON.stringify({
            action: "agent.update",
            data: change,
          }),
        });
      }
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          front_agent: agentDraft.frontAgent,
          front_mode: agentDraft.frontMode,
          work_mode: agentDraft.workMode,
          pi_work_agents: pi,
          disabled_agents: disabled,
          archived_agents: archived,
        }),
      });
      await load();
      if (agentDraft.agents[keep]) selectAgent(keep);
      toast(message || (modelChanges.length
        ? "Agent 设置与模型已保存；模型改动重启后生效"
        : "Agent 设置已保存；协作与引擎变更对新任务生效"));
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
        maxBuiltinTurns: 24,
        maxReadOnlyTurns: 8,
        frontMaxInspect: 4,
        commitEnabled: false,
        commitOutputRepo: "",
        commitMode: "manual",
        commitBaseRef: "main",
        commitAutoMerge: false,
        piTimeoutEnabled: true,
        piTimeoutSeconds: 7200,
        promptLogEnabled: false,
        timezone: "Asia/Shanghai",
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
        const initial = (agentInitial.agents || {})[agentId] || {};
        next.agents[agentId].enabled = true;
        next.agents[agentId].archived = false;
        next.agents[agentId].engine = index === 0 ? "builtin" : "pi";
        next.agents[agentId].model = initial.model || "";
        next.agents[agentId].credential = initial.credential || "";
        next.agents[agentId].provider = initial.provider || "";
      });
      agentDraft = next;
      showArchivedAgents = false;
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
      showArchivedAgents = false;
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
    renderProviders();
    $("provider-form-title").textContent = `编辑服务商 · ${name}`;
    $("provider-preset").value = "";
    $("provider-name").value = name;
    $("provider-name").disabled = true;
    $("provider-adapter").value = raw.adapter || "";
    $("provider-base-url").value = raw.base_url || "";
    providerAutoBaseUrl = "";
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
    hideModelEditor();
    renderModels();
    updateProviderHints();
    providerInitial = clone(providerSnapshot());
    updateFooter();
  }

  function newProvider() {
    editing.provider = "";
    renderProviders();
    $("provider-form-title").textContent = "新建服务商";
    $("provider-preset").value = "";
    $("provider-name").disabled = false;
    $("provider-name").value = "";
    // 新建时默认用通用 OpenAI 兼容 adapter —— 不要默认成排序第一的
    // bailian，否则用户只填名字就会收到「adapter bailian 需要 base_url」。
    const adapters = (data.system && data.system.adapters) || [];
    const names = adapters.map((item) => item.name);
    const adapter = names.includes("openai_compatible")
      ? "openai_compatible" : (names[0] || "");
    $("provider-adapter").value = adapter;
    providerAutoBaseUrl = adapterDefaultBaseUrl(adapter);
    $("provider-base-url").value = providerAutoBaseUrl;
    $("provider-timeout").value = "300";
    // 新服务商还没有自己的模型：默认模型只能是「无」。
    // 列出全部模型会让用户把别的端点的 profile 挂到这里，
    // 保存后重启时报「默认模型必须属于这个端点自己」。
    clear($("provider-default-model"));
    option($("provider-default-model"), "", "(无)");
    $("provider-request").value = "";
    hideModelEditor();
    renderModels();
    updateProviderHints();
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
    if (!payload.name) {
      toast("请填写服务商 ID（例如 opencode）。", true);
      return;
    }
    if (!payload.adapter) {
      toast("请选择 Adapter。", true);
      return;
    }
    const spec = adapterSpec(payload.adapter);
    if (!payload.base_url && spec && spec.default_base_url) {
      payload.base_url = spec.default_base_url;
    }
    if ((spec ? spec.requires_base_url : true) && !payload.base_url) {
      toast(`Adapter ${payload.adapter} 需要 Base URL。`, true);
      return;
    }
    try {
      await api("/api/settings/system", {
        method: "POST",
        body: JSON.stringify({ action: "provider.upsert", data: payload }),
      });
      await load(payload.name);
      toast("服务商已保存；重启后生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function deleteProvider(name) {
    const models = Object.entries((data.system && data.system.models) || {})
      .filter(([, raw]) => String((raw || {}).provider || "") === name)
      .map(([profile]) => profile);
    if (models.length) {
      toast(`先删除这个服务商下的模型：${models.join("、")}`, true);
      return;
    }
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
  function hideModelEditor() {
    editing.model = "";
    $("model-editor").hidden = true;
  }

  function editModel(profile) {
    const provider = editing.provider;
    const raw = (data.system.models || {})[profile] || {};
    if (String(raw.provider || "") !== provider) {
      toast("这个模型不属于当前服务商。", true);
      return;
    }
    editing.model = profile;
    $("model-profile").value = profile;
    $("model-profile").disabled = true;
    $("model-provider-name").textContent = provider;
    $("model-id").value = raw.model_id || "";
    $("model-context").value = String(raw.context_window || 600000);
    $("model-output").value = String(raw.max_output_tokens || 16384);
    $("model-reasoning").checked = !!raw.supports_reasoning;
    const levels = new Set(raw.thinking_levels || []);
    document.querySelectorAll("#model-thinking-levels input").forEach((box) => {
      box.checked = levels.has(box.value);
    });
    $("model-editor-title").textContent = `编辑模型 · ${profile}`;
    $("model-editor").hidden = false;
    modelInitial = clone(modelSnapshot());
    $("model-editor").scrollIntoView({ block: "nearest" });
  }

  function newModel() {
    if (!editing.provider) {
      toast("先保存服务商，再添加模型。", true);
      return;
    }
    editing.model = "";
    $("model-profile").disabled = false;
    $("model-profile").value = "";
    $("model-provider-name").textContent = editing.provider;
    $("model-id").value = "";
    $("model-context").value = "600000";
    $("model-output").value = "16384";
    $("model-reasoning").checked = true;
    document.querySelectorAll("#model-thinking-levels input").forEach((box) => {
      box.checked = box.value === "off";
    });
    $("model-editor-title").textContent = "添加模型";
    $("model-editor").hidden = false;
    modelInitial = clone(modelSnapshot());
    $("model-editor").scrollIntoView({ block: "nearest" });
  }

  async function saveModel() {
    if (!editing.provider) {
      toast("先保存服务商，再保存模型。", true);
      return;
    }
    const payload = {
      profile: $("model-profile").value.trim(),
      provider: editing.provider,
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
      await load(editing.provider);
      toast("模型已保存；重启后生效");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  }

  async function deleteModel(profile) {
    const provider = editing.provider;
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
      await load(provider);
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
    "system-providers",
    "system-credentials", "regular-roles",
  ]);
  const settingsContent = document.querySelector(".settings-content");
  const refreshSystemFooter = () => {
    if (systemPanes.has(currentPane)) updateFooter();
  };
  settingsContent.addEventListener("input", refreshSystemFooter);
  settingsContent.addEventListener("change", refreshSystemFooter);

  $("provider-preset").addEventListener("change", () => {
    applyProviderPreset($("provider-preset").value);
  });
  $("provider-adapter").addEventListener("change", () => {
    const next = adapterDefaultBaseUrl($("provider-adapter").value);
    const current = $("provider-base-url").value.trim();
    if (!current || current === providerAutoBaseUrl) {
      providerAutoBaseUrl = next;
      $("provider-base-url").value = next;
    }
    updateProviderHints();
    updateFooter();
  });
  $("provider-base-url").addEventListener("input", updateProviderHints);
  $("provider-new").addEventListener("click", newProvider);
  $("model-new").addEventListener("click", newModel);
  $("model-save").addEventListener("click", saveModel);
  $("model-cancel").addEventListener("click", hideModelEditor);
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
    "max-teams", "max-builtin-turns", "max-read-only-turns",
    "front-max-inspect",
    "commit-output-repo", "commit-base-ref", "pi-timeout-seconds",
    "prompt-log-enabled", "timezone",
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
    if (!agentDraft.agents[next] || agentDraft.agents[next].archived) return;
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
  $("agent-detail-provider").addEventListener("change", () => {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) return;
    const provider = $("agent-detail-provider").value;
    fillModelOptions($("agent-detail-model"), provider, "", true);
    agent.provider = provider;
    agent.model = $("agent-detail-model").value;
    updateFooter();
  });
  $("agent-detail-model").addEventListener("change", () => {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) return;
    agent.model = $("agent-detail-model").value;
    agent.provider = agentProviderOf(agent);
    updateFooter();
  });
  $("agent-detail-credential").addEventListener("change", () => {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent) return;
    agent.credential = $("agent-detail-credential").value;
    updateFooter();
  });
  $("new-agent-provider").addEventListener("change", () => {
    populateNewAgentModels();
  });
  $("agent-settings-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-agent]");
    if (item) selectAgent(item.dataset.agent);
  });
  $("agent-setting-enabled").addEventListener("change", () => {
    const agent = agentDraft.agents[selectedAgent];
    if (!agent || agent.archived || agentDraft.frontAgent === selectedAgent) {
      $("agent-setting-enabled").checked = !!agent && agent.enabled;
      return;
    }
    agent.enabled = $("agent-setting-enabled").checked;
    refreshAgentViews();
  });
  document.querySelectorAll("#agent-engine input").forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      const agent = agentDraft.agents[selectedAgent];
      if (!agent || agent.archived) return;
      agent.engine = input.value;
      refreshAgentViews();
    });
  });

  $("agent-archived-toggle").addEventListener("click", () => {
    showArchivedAgents = !showArchivedAgents;
    const ids = Object.keys(agentDraft.agents).filter(
      (agentId) => agentDraft.agents[agentId].archived === showArchivedAgents);
    selectedAgent = ids[0] || "";
    renderAgentList();
    renderAgentDetail();
  });
  $("agent-create-new").addEventListener("click", openAgentCreate);
  $("agent-create-close").addEventListener("click", closeAgentCreate);
  $("agent-create-submit").addEventListener("click", createAgent);
  $("agent-archive-btn").addEventListener("click", archiveSelectedAgent);
  $("agent-restore-btn").addEventListener("click", restoreSelectedAgent);
})();
