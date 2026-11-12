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
  };

  let state = null;
  let polling = false;
  let submitting = false;
  let toastTimer = 0;

  // 轮询重绘不能清掉用户正在拖选的文本。
  // pointerSelecting 覆盖“刚按下鼠标、选区还没形成”的瞬间。
  let pointerSelecting = false;
  let lastMessagesKey = "";
  let lastAgentsKey = "";
  let lastTasksKey = "";

  document.addEventListener("selectstart", () => {
    pointerSelecting = true;
  });
  document.addEventListener("pointerup", () => {
    pointerSelecting = false;
  });
  document.addEventListener("pointercancel", () => {
    pointerSelecting = false;
  });
  window.addEventListener("blur", () => {
    pointerSelecting = false;
  });

  function hasActiveTextSelection() {
    if (pointerSelecting) return true;
    const selection = window.getSelection();
    return !!(
      selection &&
      !selection.isCollapsed &&
      String(selection).length > 0
    );
  }

  // 与 task.js 保持同一套显示规则：<1s 用 ms，<60s 用秒，再长用分秒。
  function formatDuration(ms) {
    const value = Number(ms);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (value < 1000) return `${Math.round(value)}ms`;
    if (value < 60000) {
      const seconds = value / 1000;
      return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
    }
    const minutes = Math.floor(value / 60000);
    const seconds = Math.round((value % 60000) / 1000);
    return `${minutes}m ${seconds}s`;
  }

  function formatThinking(thinking) {
    if (!thinking || typeof thinking !== "object") return "";
    if (thinking.enabled === false) return "thinking off";
    if (thinking.enabled === true) {
      return `thinking ${thinking.effort || "on"}`;
    }
    return "thinking default";
  }

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
    const key = JSON.stringify(messages || []);
    if (key === lastMessagesKey && box.childElementCount) return;
    lastMessagesKey = key;
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
      if (message.retryable && message.job_id) {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "copy-btn retry-btn";
        retry.textContent = "重试";
        retry.addEventListener("click", async (event) => {
          event.stopPropagation();
          retry.disabled = true;
          try {
            await api("/api/retry", {
              method: "POST",
              body: JSON.stringify({ job_id: message.job_id }),
            });
            showToast("已重新提交");
            await refresh();
          } catch (error) {
            showToast(`重试失败：${error.message}`, true);
          } finally {
            retry.disabled = false;
          }
        });
        actions.appendChild(retry);
      }
      item.appendChild(meta);
      item.appendChild(text);
      const footerParts = [];
      const thinking = formatThinking(message.thinking);
      if (thinking) footerParts.push(thinking);
      const duration = formatDuration(message.duration_ms);
      if (duration) footerParts.push(`耗时 ${duration}`);
      if (footerParts.length) {
        const footer = document.createElement("div");
        footer.className = "msg-duration";
        footer.textContent = footerParts.join(" · ");
        item.appendChild(footer);
      }
      item.appendChild(actions);
      box.appendChild(item);
    });
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  function renderAgents(agents) {
    const list = agents || [];
    const key = JSON.stringify(list);
    if (key === lastAgentsKey && els.agents.childElementCount) return;
    lastAgentsKey = key;
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
      const levels = Array.isArray(agent.thinking_levels)
        ? agent.thinking_levels : [];
      if (levels.length && agent.role !== "FRONT") {
        const row = document.createElement("div");
        row.className = "agent-thinking";
        const label = document.createElement("span");
        label.textContent = "思考";
        const select = document.createElement("select");
        select.className = "agent-thinking-select";
        select.title = "Prototype 固定 max；这里设置 Engineering / Evolution";
        const auto = document.createElement("option");
        auto.value = "auto";
        auto.textContent = "Auto（阶段策略）";
        auto.selected = !agent.thinking || agent.thinking === "auto";
        select.appendChild(auto);
        levels.forEach((level) => {
          const option = document.createElement("option");
          option.value = level;
          option.textContent = level;
          option.selected = agent.thinking === level;
          select.appendChild(option);
        });
        select.addEventListener("change", async () => {
          select.disabled = true;
          try {
            await api("/api/settings", {
              method: "POST",
              body: JSON.stringify({
                agent_thinking: { [agent.agent_id]: select.value },
              }),
            });
            showToast(`${agent.agent_id} 思考强度：${select.value}`);
            await refresh();
          } catch (error) {
            showToast(`保存失败：${error.message}`, true);
            select.disabled = false;
          }
        });
        row.appendChild(label);
        row.appendChild(select);
        card.appendChild(row);
      }
      els.agents.appendChild(card);
    });
  }

  function renderTasks(tasks) {
    const list = tasks || [];
    const key = JSON.stringify(list);
    if (key === lastTasksKey && els.tasks.childElementCount) return;
    lastTasksKey = key;
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
        // 正在选择卡片文字时不跳转，否则拖选会在 mouseup 时被当成点击。
        if (hasActiveTextSelection()) return;
        // 同一标签页打开：避免多个 AIEOS 页面并存，返回按钮也回到同一个入口。
        window.location.assign(
          `/task.html?task_id=${encodeURIComponent(task.task_id)}`,
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

  function render() {
    if (!state) return;
    els.frontBadge.textContent = `Front: ${state.front_agent || "—"}`;
    els.runningBadge.textContent = `Running: ${state.running_jobs || 0}`;
    // 有选区时冻结列表重绘；用户点掉选区后，下一次轮询会补上最新数据。
    if (hasActiveTextSelection()) return;
    renderMessages(state.messages || []);
    renderAgents(state.agents);
    renderTasks(state.tasks);
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

  /* events */
  els.submit.addEventListener("click", submitPrompt);
  els.prompt.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      submitPrompt();
    }
  });
  refresh();
  setInterval(refresh, 1200);
})();
