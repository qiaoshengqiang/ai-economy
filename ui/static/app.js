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

  function showToast(text, isError = false) {
    els.toast.textContent = String(text || "");
    els.toast.classList.toggle("error", !!isError);
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 6500);
  }

  async function copyText(text) {
    const value = String(text || "");
    if (!value) return false;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch (e) {
        // 某些宿主不会给 Clipboard 权限，走 fallback。
      }
    }
    try {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      area.style.top = "0";
      document.body.appendChild(area);
      area.select();
      area.setSelectionRange(0, area.value.length);
      const ok = document.execCommand("copy");
      area.remove();
      return !!ok;
    } catch (e) {
      return false;
    }
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
      const actions = document.createElement("div");
      actions.className = "msg-actions";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-btn";
      copy.textContent = "复制";
      copy.setAttribute("aria-label", "复制这条消息");
      copy.addEventListener("click", async (event) => {
        event.stopPropagation();
        const ok = await copyText(message.text || "");
        if (ok) {
          copy.textContent = "已复制";
          copy.classList.add("copied");
          setTimeout(() => {
            copy.textContent = "复制";
            copy.classList.remove("copied");
          }, 1200);
        } else {
          showToast("复制失败，请手动选择文本", true);
        }
      });
      actions.appendChild(copy);
      item.appendChild(meta);
      item.appendChild(text);
      item.appendChild(actions);
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
      card.title = "在新标签页打开执行明细";
      card.addEventListener("click", () => {
        window.open(
          `/task.html?task_id=${encodeURIComponent(task.task_id)}`,
          "_blank",
          "noopener",
        );
      });
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
      const label = document.createElement("div");
      label.className = "task-prompt-label";
      label.textContent = "Front → Work 提示词";
      const desc = document.createElement("div");
      desc.className = "task-desc";
      desc.textContent = task.description || "";
      const meta = document.createElement("div");
      meta.className = "task-meta";
      const leader = document.createElement("span");
      leader.textContent = `执行 Agent: ${task.leader || "—"}`;
      const members = document.createElement("span");
      members.textContent = `参与者: ${(task.members || []).join(", ") || "—"}`;
      const iteration = document.createElement("span");
      iteration.textContent = `迭代 ${task.iteration || 1}`;
      meta.appendChild(leader);
      meta.appendChild(members);
      meta.appendChild(iteration);
      card.appendChild(top);
      card.appendChild(label);
      card.appendChild(desc);
      card.appendChild(meta);
      els.tasks.appendChild(card);
    });
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
    renderMessages(state.messages || []);
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
