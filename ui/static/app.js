/* AIEOS UI —— 无构建、无依赖 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    frontBadge: $("front-badge"),
    runningBadge: $("running-badge"),
    messages: $("messages"),
    prompt: $("prompt"),
    submit: $("submit-btn"),
    toast: $("toast"),
    agents: $("agents"),
    agentsCount: $("agents-count"),
    tasks: $("tasks"),
    tasksCount: $("tasks-count"),
    settingsBtn: $("settings-btn"),
    settingsModal: $("settings-modal"),
    settingsClose: $("settings-close"),
    settingsCancel: $("settings-cancel"),
    settingsSave: $("settings-save"),
    frontSelect: $("front-select"),
    maxTeams: $("max-teams"),
  };

  let state = null;
  let polling = false;
  let submitting = false;
  let toastTimer = 0;
  // 点击 Task 卡片后生成的日志消息（只在本次页面会话里保留）。
  let taskLogs = [];

  function showToast(text, isError = false) {
    els.toast.textContent = String(text || "");
    els.toast.classList.toggle("error", !!isError);
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 6500);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok || (data && data.ok === false)) {
      const message = (data && (data.error || data.message)) ||
        `HTTP ${response.status}`;
      throw new Error(message);
    }
    return data;
  }

  function renderMessages(messages) {
    const box = els.messages;
    const atBottom =
      box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    box.innerHTML = "";
    if (!messages || !messages.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "还没有对话。把你的需求发给 Front Agent。";
      box.appendChild(empty);
      return;
    }
    messages.forEach((message) => {
      const item = document.createElement("div");
      item.className = `msg ${message.role || "system"}`;
      const meta = document.createElement("span");
      meta.className = "meta";
      const role = message.role === "human" ? "你" :
        message.role === "front" ? "Front Agent" :
        message.role === "task" ? "Task log" : "系统";
      meta.textContent = `${role}${message.task_id ? " · " + message.task_id : ""}`;
      const text = document.createElement("div");
      text.textContent = message.text || "";
      item.appendChild(meta);
      item.appendChild(text);
      box.appendChild(item);
    });
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function renderAgents(agents) {
    const list = agents || [];
    els.agentsCount.textContent = `${list.length} 个`;
    els.agents.innerHTML = "";
    list.forEach((agent) => {
      const card = document.createElement("div");
      card.className = "agent";
      const top = document.createElement("div");
      top.className = "agent-top";
      const dot = document.createElement("span");
      dot.className = `dot ${agent.status || ""}`;
      const id = document.createElement("span");
      id.className = "agent-id";
      id.textContent = agent.agent_id;
      const role = document.createElement("span");
      role.className = "agent-role";
      role.textContent = agent.role || "";
      top.appendChild(dot);
      top.appendChild(id);
      top.appendChild(role);
      card.appendChild(top);
      const task = document.createElement("div");
      task.className = "agent-task";
      task.textContent = agent.task_id
        ? `task ${agent.task_id}`
        : (agent.has_brain ? "IDLE · ready" : "no brain");
      card.appendChild(task);
      els.agents.appendChild(card);
    });
  }

  function renderTasks(tasks) {
    const list = tasks || [];
    els.tasksCount.textContent = `${list.length} 个`;
    els.tasks.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "还没有 Task。";
      els.tasks.appendChild(empty);
      return;
    }
    list.forEach((task) => {
      const card = document.createElement("div");
      card.className = `task-card clickable ${task.status || ""}`;
      card.addEventListener("click", () => openTask(task.task_id));
      const top = document.createElement("div");
      top.className = "task-top";
      const id = document.createElement("span");
      id.className = "task-id";
      id.textContent = task.task_id;
      const status = document.createElement("span");
      status.className = "task-status";
      status.textContent = task.status || "";
      top.appendChild(id);
      top.appendChild(status);
      const desc = document.createElement("div");
      desc.className = "task-desc";
      desc.textContent = task.description || "";
      const meta = document.createElement("div");
      meta.className = "task-meta";
      const leader = document.createElement("span");
      leader.textContent = `Leader: ${task.leader || "—"}`;
      const members = document.createElement("span");
      members.textContent = `成员: ${(task.members || []).join(", ") || "—"}`;
      const iteration = document.createElement("span");
      iteration.textContent = `迭代 ${task.iteration || 1}`;
      meta.appendChild(leader);
      meta.appendChild(members);
      meta.appendChild(iteration);
      card.appendChild(top);
      card.appendChild(desc);
      card.appendChild(meta);
      els.tasks.appendChild(card);
    });
  }

  function formatTaskLog(detail) {
    const task = detail.task || {};
    const job = detail.job || {};
    const llm = detail.llm || {};
    const lines = [
      `【任务 ${task.task_id || "?"}】${task.status || "?"} · ${task.mode || "?"} · iteration ${task.iteration || 1}`,
      `Leader: ${task.creator || "—"}　Contractor: ${task.contractor || "—"}`,
      `成员: ${(task.participants || []).join(", ") || "—"}`,
      `描述: ${task.description || "—"}`,
    ];
    if (job.status) {
      lines.push(`Job: ${job.status}${job.error ? " · " + job.error : ""}`);
    }
    if (detail.executions && detail.executions.length) {
      lines.push("", "执行历史:");
      detail.executions.forEach((item) => {
        let outcome = "";
        if (item.verification) {
          outcome = item.verification.passed ? " · 验收通过" : " · 验收不通过";
        }
        if (item.result && item.result.summary) {
          outcome += ` · ${item.result.summary}`;
        }
        lines.push(
          `- ${item.created_at || ""} ${item.event_type || ""} by ` +
          `${item.from_agent || "?"} → ${item.status_after || "?"}${outcome}`);
      });
    }
    if (detail.workspace_files && detail.workspace_files.length) {
      lines.push("", "Workspace 文件:");
      detail.workspace_files.forEach((file) => {
        const rev = file.revision == null ? "" : ` R${file.revision}`;
        const chars = file.chars == null ? "" : ` · ${file.chars} chars`;
        lines.push(`- ${file.path}${rev}${chars}`);
      });
    }
    if (detail.public_memory) {
      lines.push("", "公共工作记忆（抽象架构）:", detail.public_memory);
    }
    lines.push(
      "",
      `LLM: ${llm.calls || 0} calls · ` +
      `${llm.input_tokens || 0} in · ${llm.output_tokens || 0} out · ` +
      `${llm.cost_ai || 0} AIC`);
    if (job.summary) lines.push(`结果: ${job.summary}`);
    return lines.join("\n");
  }

  async function openTask(taskId) {
    try {
      const detail = await api(`/api/task/${encodeURIComponent(taskId)}`);
      taskLogs = taskLogs.filter((item) => item.task_id !== taskId);
      taskLogs.push({
        role: "task",
        text: formatTaskLog(detail),
        task_id: taskId,
        at: new Date().toISOString(),
      });
      render();
      // 打开任务日志后滚到输出框底部。
      els.messages.scrollTop = els.messages.scrollHeight;
    } catch (error) {
      showToast(`任务读取失败：${error.message}`, true);
    }
  }

  function renderSettings() {
    if (!state) return;
    const front = state.front_agent || "";
    const options = (state.agents || []).map((agent) => agent.agent_id);
    els.frontSelect.innerHTML = "";
    options.forEach((id) => {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      option.selected = id === front;
      els.frontSelect.appendChild(option);
    });
    els.maxTeams.value = String(state.settings?.max_parallel_teams || 3);
  }

  function render() {
    if (!state) return;
    els.frontBadge.textContent = `Front: ${state.front_agent || "—"}`;
    els.runningBadge.textContent = `Running: ${state.running_jobs || 0}`;
    renderMessages([...(state.messages || []), ...taskLogs]);
    renderAgents(state.agents);
    renderTasks(state.tasks);
    // 设置弹窗打开时不要被轮询覆盖用户正在编辑的值。
    if (els.settingsModal.hidden) renderSettings();
  }

  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      state = await api("/api/state");
      render();
    } catch (error) {
      showToast(`状态读取失败：${error.message}`, true);
    } finally {
      polling = false;
    }
  }

  async function submitPrompt() {
    const text = els.prompt.value.trim();
    if (!text) {
      showToast("请先输入内容");
      return;
    }
    submitting = true;
    els.submit.disabled = true;
    try {
      await api("/api/submit", {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      els.prompt.value = "";
      await refresh();
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    } finally {
      submitting = false;
      els.submit.disabled = false;
      els.prompt.focus();
    }
  }

  function openSettings() {
    renderSettings();
    els.settingsModal.hidden = false;
  }

  function closeSettings() {
    els.settingsModal.hidden = true;
  }

  async function saveSettings() {
    try {
      const settings = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          front_agent: els.frontSelect.value,
          max_parallel_teams: Number(els.maxTeams.value || 3),
        }),
      });
      state = state || {};
      state.front_agent = settings.settings?.front_agent || els.frontSelect.value;
      state.settings = settings.settings;
      closeSettings();
      await refresh();
      showToast("设置已保存");
    } catch (error) {
      showToast(`保存失败：${error.message}`, true);
    }
  }

  /* events */
  els.submit.addEventListener("click", submitPrompt);
  els.prompt.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      submitPrompt();
    }
  });
  els.settingsBtn.addEventListener("click", openSettings);
  els.settingsClose.addEventListener("click", closeSettings);
  els.settingsCancel.addEventListener("click", closeSettings);
  els.settingsSave.addEventListener("click", saveSettings);
  els.settingsModal.addEventListener("click", (event) => {
    if (event.target === els.settingsModal) closeSettings();
  });

  refresh();
  setInterval(refresh, 1200);
})();
