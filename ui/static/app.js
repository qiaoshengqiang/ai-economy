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
  // 展开的 Task 卡片与已加载的执行明细（页面会话内缓存）。
  const expandedTasks = new Set();
  const taskDetails = new Map();

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

  function _el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function _clipText(text, limit = 60000) {
    const value = String(text || "");
    if (value.length <= limit) return value;
    return value.slice(0, limit) + `\n…（截断，共 ${value.length} 字符）`;
  }

  function _pre(text, className = "phase-output") {
    const node = document.createElement("pre");
    node.className = className;
    node.textContent = _clipText(text);
    return node;
  }

  function _actionLine(action, result) {
    const type = action.type || action.action_type || "?";
    const params = action.parameters || {};
    const target = params.path || params.query || params.item_id || "";
    let line = `${type}${target ? " " + target : ""}`;
    if (!result) {
      line += " · 未执行";
    } else if (result.success) {
      line += " · ok";
    } else {
      line += " · failed";
      if (result.error) line += ` · ${result.error}`;
    }
    if (result && result.output) {
      if (result.output.chars != null) line += ` · ${result.output.chars} chars`;
      if (result.output.revision != null) line += ` · R${result.output.revision}`;
      if (result.output.exit_code != null) line += ` · exit=${result.output.exit_code}`;
    }
    return line;
  }

  function _detailSection(parent, title) {
    const block = _el("section", "detail-section");
    block.appendChild(_el("div", "detail-title", title));
    parent.appendChild(block);
    return block;
  }

  function renderTaskDetail(container, detail) {
    container.innerHTML = "";
    const task = detail.task || {};
    const job = detail.job || {};
    const llm = detail.llm || {};

    const head = _el("div", "detail-meta");
    head.textContent =
      `${task.status || "?"} · ${task.mode || "?"} · ` +
      `iteration ${task.iteration || 1} · ${task.creator || "—"}`;
    container.appendChild(head);
    if (job.status) {
      container.appendChild(_el(
        "div", "detail-meta",
        `Job: ${job.status}${job.error ? " · " + job.error : ""}`));
    }

    const rounds = Array.isArray(detail.rounds) ? detail.rounds : [];
    const roundsBlock = _detailSection(container, `执行轮次（${rounds.length}）`);
    if (!rounds.length) {
      roundsBlock.appendChild(_el("div", "muted", "旧任务没有轮次记录。"));
    }
    rounds.forEach((round) => {
      const block = document.createElement("details");
      block.className = "round-block";
      block.open = true;
      const summary = document.createElement("summary");
      summary.textContent =
        `Round ${round.round || "?"} · ` +
        `${round.status_before || "?"} → ${round.status_after || "?"} · ` +
        `${round.tokens || 0} tokens`;
      block.appendChild(summary);

      (round.segments || []).forEach((seg) => {
        const phase = _el(
          "div", `phase-block ${seg.phase || ""}`);
        const title = _el("div", "phase-title");
        let label = seg.phase || "phase";
        if (seg.truncated) label += " · truncated";
        if (seg.finish_reason) label += ` · ${seg.finish_reason}`;
        if (seg.tokens != null) label += ` · ${seg.tokens} tokens`;
        title.textContent = label;
        phase.appendChild(title);
        if (seg.content) phase.appendChild(_pre(seg.content));

        const actions = seg.actions || [];
        const results = seg.results || [];
        if (actions.length) {
          const list = _el("div", "action-list");
          actions.forEach((action, index) => {
            const row = _el(
              "div", "action-row", _actionLine(action, results[index]));
            list.appendChild(row);
            if (results[index] && results[index].output) {
              const out = document.createElement("details");
              out.className = "action-output";
              const outSummary = document.createElement("summary");
              outSummary.textContent = "result";
              out.appendChild(outSummary);
              out.appendChild(_pre(
                JSON.stringify(results[index].output, null, 2)));
              list.appendChild(out);
            }
          });
          phase.appendChild(list);
        }
        block.appendChild(phase);
      });

      const hasSummarySegment = (round.segments || []).some(
        (seg) => seg.phase === "summary");
      if (round.summary && !hasSummarySegment) {
        const summaryBlock = _el("div", "phase-block summary");
        summaryBlock.appendChild(_el("div", "phase-title", "summary"));
        summaryBlock.appendChild(_pre(round.summary));
        block.appendChild(summaryBlock);
      }
      roundsBlock.appendChild(block);
    });

    const files = detail.workspace_files || [];
    const fileBlock = _detailSection(container, `Workspace 文件（${files.length}）`);
    files.forEach((file) => {
      const rev = file.revision == null ? "" : ` R${file.revision}`;
      const chars = file.chars == null ? "" : ` · ${file.chars} chars`;
      fileBlock.appendChild(_el("div", "file-row", `${file.path}${rev}${chars}`));
    });

    if (detail.public_memory) {
      const memoryBlock = _detailSection(container, "公共工作记忆");
      memoryBlock.appendChild(_pre(detail.public_memory));
    }

    const execs = detail.executions || [];
    if (execs.length) {
      const execBlock = _detailSection(container, "执行历史");
      execs.forEach((item) => {
        let outcome = "";
        if (item.verification) {
          outcome = item.verification.passed ? " · 验收通过" : " · 验收不通过";
        }
        if (item.result && item.result.summary) {
          outcome += ` · ${item.result.summary}`;
        }
        execBlock.appendChild(_el(
          "div", "file-row",
          `${item.created_at || ""} ${item.event_type || ""} by ` +
          `${item.from_agent || "?"} → ${item.status_after || "?"}${outcome}`));
      });
    }

    const llmBlock = _detailSection(container, "LLM");
    llmBlock.appendChild(_el(
      "div", "file-row",
      `${llm.calls || 0} calls · ${llm.input_tokens || 0} in · ` +
      `${llm.output_tokens || 0} out · ${llm.cost_ai || 0} AIC`));
    if (job.summary) {
      llmBlock.appendChild(_el("div", "file-row", `结果：${job.summary}`));
    }
  }

  async function loadTaskDetail(taskId) {
    try {
      const detail = await api(`/api/task/${encodeURIComponent(taskId)}`);
      taskDetails.set(taskId, detail);
      renderTasks(state ? state.tasks : []);
    } catch (error) {
      showToast(`任务读取失败：${error.message}`, true);
    }
  }

  function toggleTask(taskId) {
    if (expandedTasks.has(taskId)) {
      expandedTasks.delete(taskId);
    } else {
      expandedTasks.add(taskId);
      if (!taskDetails.has(taskId)) {
        loadTaskDetail(taskId);
      }
    }
    renderTasks(state ? state.tasks : []);
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
      const expanded = expandedTasks.has(task.task_id);
      card.className =
        `task-card clickable ${task.status || ""}${expanded ? " expanded" : ""}`;
      card.addEventListener("click", () => toggleTask(task.task_id));
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
      leader.textContent = `执行 Agent: ${task.leader || "—"}`;
      const members = document.createElement("span");
      members.textContent = `参与者: ${(task.members || []).join(", ") || "—"}`;
      const iteration = document.createElement("span");
      iteration.textContent = `迭代 ${task.iteration || 1}`;
      meta.appendChild(leader);
      meta.appendChild(members);
      meta.appendChild(iteration);
      card.appendChild(top);
      card.appendChild(desc);
      card.appendChild(meta);

      if (expanded) {
        const detail = document.createElement("div");
        detail.className = "task-detail";
        detail.addEventListener("click", (event) => event.stopPropagation());
        const data = taskDetails.get(task.task_id);
        if (data) {
          renderTaskDetail(detail, data);
        } else {
          detail.textContent = "加载执行明细…";
        }
        card.appendChild(detail);
      }
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
