/* AIEOS Task 详情页：轮次 / 思考 / 输出 / 动作 / 结果。 */
(() => {
  "use strict";

  const taskId = new URLSearchParams(location.search).get("task_id") || "";
  const els = {
    status: document.getElementById("status"),
    meta: document.getElementById("task-meta"),
    brief: document.getElementById("task-brief"),
    timeline: document.getElementById("timeline"),
    workspace: document.getElementById("workspace"),
    memory: document.getElementById("public-memory"),
    llm: document.getElementById("llm"),
    executions: document.getElementById("executions"),
  };
  let timer = 0;
  let lastSignature = "";
  let pointerSelecting = false;

  const TERMINAL = new Set(["COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"]);

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

  // 与 app.js 保持同一套显示规则：<1s 用 ms，<60s 用秒，再长用分秒。
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
    // null = 新日志里明确的「没有设置，用 provider 默认」；
    // 旧日志没有这个键，调用方不会调用本函数。
    if (!thinking || typeof thinking !== "object") return "thinking default";
    if (thinking.enabled === false) return "thinking off";
    if (thinking.enabled === true) {
      return `thinking ${thinking.effort || "on"}`;
    }
    return "thinking default";
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function clip(text, limit = 60000) {
    const value = String(text == null ? "" : text);
    if (value.length <= limit) return value;
    return value.slice(0, limit) + `\n…（截断，共 ${value.length} 字符）`;
  }

  function pre(text, className = "") {
    const node = document.createElement("pre");
    if (className) node.className = className;
    node.textContent = clip(text);
    return node;
  }

  function json(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return String(value);
    }
  }

  function techItem(parent, key, value) {
    const item = el("div", "tech-item");
    item.appendChild(el("b", "", key));
    item.appendChild(document.createTextNode(
      value == null || value === "" ? "—" : String(value)));
    parent.appendChild(item);
  }

  function fact(parent, label, value, title = "") {
    if (value == null || value === "") return;
    const item = el("span", "fact");
    item.appendChild(el("b", "", label));
    item.appendChild(document.createTextNode(String(value)));
    if (title) item.title = title;
    parent.appendChild(item);
  }

  function statusTone(status) {
    if (status === "COMPLETED") return "good";
    if (["FAILED", "INTERRUPTED", "CANCELLED"].includes(status)) return "bad";
    if (status === "IN_PROGRESS") return "warn";
    return "";
  }

  function baseName(path) {
    const parts = String(path || "").split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }

  function badge(text, tone = "") {
    return el("span", `badge ${tone}`.trim(), text);
  }

  function targetOf(action) {
    const params = (action && action.parameters) || {};
    return params.path || params.query || params.tool || params.item_id || "";
  }

  function renderMeta(data) {
    const task = data.task || {};
    const job = data.job;
    els.meta.innerHTML = "";

    const top = el("div", "hero-top");
    const main = el("div", "hero-main");
    const eyebrow = [
      task.mode || "SOLO",
      `iteration ${task.iteration || 1}`,
      task.work_profile || "",
    ].filter(Boolean).join(" · ");
    main.appendChild(el("div", "hero-eyebrow", eyebrow));
    main.appendChild(el("h1", "hero-title", task.task_id || taskId));
    top.appendChild(main);

    const status = task.status || "—";
    const pill = el("span", `hero-status ${statusTone(status)}`.trim());
    pill.appendChild(el("span", "dot", "●"));
    pill.appendChild(document.createTextNode(status));
    top.appendChild(pill);
    els.meta.appendChild(top);

    const facts = el("div", "facts");
    fact(facts, "Agent", task.creator || "");
    fact(facts, "Role", task.role || "");
    if (task.workspace_mode === "project") {
      fact(facts, "Workspace",
        `Project · ${baseName(task.project_path) || "项目"}`,
        task.project_path || "");
    } else {
      fact(facts, "Workspace", "Task Workspace");
    }
    fact(facts, "轮次", task.iteration || 1);
    if (job) {
      const jobText = job.status || "";
      const jobError = job.error ? ` · ${job.error}` : "";
      fact(facts, "Job", `${jobText}${jobError}`);
      const jobDuration = formatDuration(job.duration_ms);
      if (jobDuration) fact(facts, "耗时", jobDuration);
      const frontDuration = formatDuration(job.front_ms);
      if (frontDuration) fact(facts, "Front", frontDuration);
    }
    if (facts.childElementCount) els.meta.appendChild(facts);

    const details = document.createElement("details");
    details.className = "tech-info";
    details.appendChild(el("summary", "", "技术信息"));
    const grid = el("div", "tech-grid");
    techItem(grid, "creator", task.creator);
    techItem(grid, "contractor", task.contractor);
    techItem(grid, "participants", (task.participants || []).join(", "));
    techItem(grid, "workspace_id", task.workspace_id);
    techItem(grid, "workspace_mode", task.workspace_mode);
    if (task.project_path) techItem(grid, "project_path", task.project_path);
    techItem(grid, "created", task.created_at);
    techItem(grid, "updated", task.updated_at);
    if (job) {
      techItem(grid, "job", job.job_id);
      if (job.front_thinking && typeof job.front_thinking === "object") {
        techItem(grid, "Front 思考", formatThinking(job.front_thinking));
      }
    }
    details.appendChild(grid);
    els.meta.appendChild(details);
  }

  function briefList(label, items) {
    if (!items || !items.length) return null;
    const panel = el("div", "brief-panel");
    panel.appendChild(el("div", "brief-label", label));
    const list = el("ul", "brief-list");
    items.forEach((item) => list.appendChild(el("li", "", item)));
    panel.appendChild(list);
    return panel;
  }

  function renderBrief(data) {
    const task = data.task || {};
    const brief = data.brief || {};
    const constraints = Array.isArray(brief.constraints)
      ? brief.constraints : [];
    const acceptance = (
      Array.isArray(brief.acceptance_expectations) &&
      brief.acceptance_expectations.length
    ) ? brief.acceptance_expectations : (task.criteria || []);
    const deliverables = Array.isArray(brief.deliverables)
      ? brief.deliverables : [];
    const goal = brief.goal || task.description || "";
    const taskText = brief.task_text || task.description || "";
    const structured = !!(
      constraints.length || deliverables.length ||
      (Array.isArray(brief.acceptance_expectations) &&
       brief.acceptance_expectations.length));

    els.brief.innerHTML = "";
    els.brief.appendChild(el("div", "section-title",
      structured ? "Front → Work" : "任务说明"));
    if (structured) {
      const lead = el("div", "brief-lead");
      lead.appendChild(badge("交付任务说明"));
      lead.appendChild(el("span", "muted-note",
        "Front 自主模式下交给 Worker 的任务上下文"));
      els.brief.appendChild(lead);
    }
    if (goal) els.brief.appendChild(el("div", "brief-goal", goal));

    const grid = el("div", "brief-grid");
    const constraintPanel = briefList("约束", constraints);
    const acceptancePanel = briefList("验收条件", acceptance);
    if (constraintPanel) grid.appendChild(constraintPanel);
    if (acceptancePanel) grid.appendChild(acceptancePanel);
    if (grid.childElementCount) els.brief.appendChild(grid);

    const extras = [];
    const deliverablePanel = briefList("交付物", deliverables);
    if (deliverablePanel) extras.push(deliverablePanel);
    if (taskText && taskText !== goal) {
      const panel = el("div", "brief-panel");
      panel.appendChild(el("div", "brief-label", "task_text"));
      panel.appendChild(pre(taskText));
      extras.push(panel);
    }
    if (extras.length) {
      const details = document.createElement("details");
      details.className = "brief-extra";
      details.appendChild(el("summary", "", "交付物 / 原始 task_text"));
      extras.forEach((node) => details.appendChild(node));
      els.brief.appendChild(details);
    }
    if (!goal && !grid.childElementCount) {
      els.brief.appendChild(el("div", "empty", "（没有任务说明）"));
    }
  }

  function renderAction(action, result) {
    const box = el("div", "action");
    const head = el("div", "action-head");
    head.appendChild(el("span", "action-type",
      action.action_type || action.type || "?"));
    const target = targetOf(action);
    if (target) head.appendChild(el("span", "action-target", target));
    const toolDuration = result && result.output
      ? formatDuration(result.output.duration_ms) : "";
    if (toolDuration) head.appendChild(badge(toolDuration));
    if (!result) {
      head.appendChild(badge("未执行", "warn"));
    } else if (result.success) {
      head.appendChild(badge("ok", "ok"));
    } else {
      head.appendChild(badge("failed", "bad"));
    }
    box.appendChild(head);

    const params = action.parameters || {};
    if (Object.keys(params).length) {
      const d = document.createElement("details");
      d.appendChild(el("summary", "", "parameters"));
      d.appendChild(pre(json(params)));
      box.appendChild(d);
    }
    if (result) {
      const d = document.createElement("details");
      d.appendChild(el("summary", "", "result"));
      const payload = {
        success: result.success,
        error: result.error,
        metadata: result.metadata,
        output: result.output,
      };
      d.appendChild(pre(json(payload)));
      box.appendChild(d);
    }
    return box;
  }

  function renderSegment(seg) {
    const phase = el("div", `phase ${seg.phase || ""}`.trim());
    const head = el("div", "phase-head");
    head.appendChild(el("span", "phase-name", seg.phase || "phase"));
    if (seg.thinking !== undefined) {
      head.appendChild(badge(formatThinking(seg.thinking)));
    }
    if (seg.tokens != null) head.appendChild(badge(`${seg.tokens} tokens`));
    const latency = formatDuration(seg.latency_ms);
    if (latency) head.appendChild(badge(latency));
    if (seg.finish_reason) head.appendChild(badge(seg.finish_reason));
    if (seg.truncated) head.appendChild(badge("truncated", "warn"));
    phase.appendChild(head);

    const content = String(seg.content || "");
    const decision = seg.decision || {};
    const modelReasoning = String(seg.model_reasoning || "");
    const decisionReasoning = String(decision.reasoning || "");

    // thinking = provider 的隐藏推理。没有就只显示 output / reasoning，
    // 绝不再把整段回复原文同时当成 thinking 和 output。
    if (modelReasoning.trim()) {
      phase.appendChild(el("div", "thinking-label", "thinking"));
      phase.appendChild(pre(modelReasoning, "thinking"));
    } else if (
      decisionReasoning.trim() &&
      decisionReasoning.trim() !== content.trim() &&
      decision.kind !== "reply"
    ) {
      phase.appendChild(el("div", "thinking-label", "reasoning"));
      phase.appendChild(pre(decisionReasoning, "thinking"));
    }
    if (content) {
      phase.appendChild(el("div", "thinking-label", "output"));
      phase.appendChild(pre(content));
    }

    const actions = seg.actions || [];
    const results = seg.results || [];
    if (actions.length) {
      phase.appendChild(el("div", "thinking-label", "actions"));
      const list = el("div", "action-list");
      actions.forEach((action, index) => {
        list.appendChild(renderAction(action, results[index]));
      });
      phase.appendChild(list);
    }
    return phase;
  }

  function renderTimeline(data) {
    const rounds = Array.isArray(data.rounds) ? data.rounds : [];
    // 轮询会重建 timeline；先保存用户手动收起/展开的状态。
    const previousOpen = new Map();
    els.timeline.querySelectorAll("details.round[data-round-key]")
      .forEach((node) => {
        previousOpen.set(node.dataset.roundKey, node.open);
      });
    els.timeline.innerHTML = "";
    if (!rounds.length) {
      els.timeline.appendChild(el("div", "empty",
        "还没有轮次记录。旧任务可能没有落盘 phase 明细。"));
      return;
    }
    let lastRunId = null;
    rounds.forEach((round) => {
      const runId = String(round.run_id || "");
      if (runId && runId !== lastRunId) {
        // 同一 Task 可以 resume 多次；round 编号会从 1 重新开始。
        // run 分隔让「两个 Round 1」不会看起来像数据重复。
        const iteration = round.iteration == null
          ? "" : ` · iteration ${round.iteration}`;
        els.timeline.appendChild(el(
          "div", "run-divider", `Run ${runId}${iteration}`));
        lastRunId = runId;
      }
      const block = document.createElement("details");
      block.className = `round${round.cognition ? " cognition" : ""}`;
      const roundKey = [
        runId || "run",
        round.iteration == null ? "" : round.iteration,
        round.round == null ? "" : round.round,
        round.phase || (round.cognition ? "cognition" : "work"),
      ].join("|");
      block.dataset.roundKey = roundKey;
      // 默认收起；用户手动展开 / 收起后，后续轮询保持该状态。
      block.open = previousOpen.has(roundKey)
        ? previousOpen.get(roundKey)
        : false;
      const summary = document.createElement("summary");
      const roundLatency = round.latency_ms != null
        ? round.latency_ms
        : (round.segments || []).reduce(
          (sum, seg) => sum + (Number(seg.latency_ms) || 0), 0);
      const roundTime = formatDuration(roundLatency);
      const timeSuffix = roundTime ? ` · ${roundTime}` : "";
      summary.textContent = round.cognition
        ? `任务认知 · ${round.tokens || 0} tokens${timeSuffix}`
        : `Round ${round.round || "?"} · ` +
          `${round.status_before || "?"} → ${round.status_after || "?"} · ` +
          `${round.tokens || 0} tokens${timeSuffix}`;
      block.appendChild(summary);

      const body = el("div", "round-body");
      (round.segments || []).forEach((seg) => {
        body.appendChild(renderSegment(seg));
      });

      const hasSummarySegment = (round.segments || []).some(
        (seg) => seg.phase === "summary");
      if (round.summary && !hasSummarySegment) {
        const phase = el("div", "phase summary");
        phase.appendChild(el("div", "phase-head",
          "summary"));
        phase.appendChild(pre(round.summary));
        body.appendChild(phase);
      }
      block.appendChild(body);
      els.timeline.appendChild(block);
    });
  }

  function renderWorkspace(data) {
    const files = data.workspace_files || [];
    els.workspace.innerHTML = "";
    els.workspace.appendChild(el("div", "section-title",
      `Workspace 文件（${files.length}）`));
    if (!files.length) {
      els.workspace.appendChild(el("div", "empty", "（没有文件）"));
      return;
    }
    files.forEach((file) => {
      const rev = file.revision == null ? "" : ` R${file.revision}`;
      const chars = file.chars == null ? "" : ` · ${file.chars} chars`;
      els.workspace.appendChild(el("div", "file-row",
        `${file.path}${rev}${chars}`));
    });
  }

  function renderMemory(data) {
    els.memory.innerHTML = "";
    if (!data.public_memory) {
      els.memory.hidden = true;
      return;
    }
    els.memory.hidden = false;
    els.memory.appendChild(el("div", "section-title", "公共工作记忆"));
    els.memory.appendChild(pre(data.public_memory));
  }

  function renderLLM(data) {
    const llm = data.llm || {};
    els.llm.innerHTML = "";
    els.llm.appendChild(el("div", "section-title", "LLM"));
    let line =
      `${llm.calls || 0} calls · ${llm.input_tokens || 0} in · ` +
      `${llm.output_tokens || 0} out · ${llm.cost_ai || 0} AIC`;
    const totalTime = formatDuration(llm.latency_ms);
    const avgTime = formatDuration(llm.avg_latency_ms);
    if (totalTime) line += ` · 总耗时 ${totalTime}`;
    if (avgTime) line += ` · 平均 ${avgTime}/call`;
    els.llm.appendChild(el("div", "file-row", line));
  }

  function renderExecutions(data) {
    const items = data.executions || [];
    els.executions.innerHTML = "";
    els.executions.appendChild(el("div", "section-title", "执行历史"));
    if (!items.length) {
      els.executions.appendChild(el("div", "empty", "（没有执行事件）"));
      return;
    }
    items.forEach((item) => {
      let text =
        `${item.created_at || ""} ${item.event_type || ""} by ` +
        `${item.from_agent || "?"} → ${item.status_after || "?"}`;
      if (item.verification) {
        text += item.verification.passed ? " · 验收通过" : " · 验收不通过";
      }
      if (item.result && item.result.summary) {
        text += ` · ${item.result.summary}`;
      }
      els.executions.appendChild(el("div", "file-row", text));
    });
  }

  function render(data) {
    const task = data.task || {};
    document.title = `AIEOS · ${task.task_id || taskId}`;
    const taskStatus = task.status || "?";
    const jobStatus = data.job ? data.job.status : "";
    els.status.textContent = (
      jobStatus && jobStatus !== taskStatus
        ? `${taskStatus} · ${jobStatus}`
        : taskStatus
    );
    renderMeta(data);
    renderBrief(data);
    renderTimeline(data);
    renderWorkspace(data);
    renderMemory(data);
    renderLLM(data);
    renderExecutions(data);
  }

  async function load() {
    if (!taskId) {
      els.status.textContent = "缺少 task_id";
      els.timeline.appendChild(el("div", "empty", "URL 里没有 task_id。"));
      return;
    }
    try {
      const response = await fetch(
        `/api/task/${encodeURIComponent(taskId)}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = await response.json();
      const signature = JSON.stringify(data);
      // 有选区时不要把整页 DOM 换掉；选区清掉后下一次轮询再补渲染。
      const selecting = hasActiveTextSelection();
      if (signature !== lastSignature && !selecting) {
        lastSignature = signature;
        render(data);
      }
      const status = data.task && data.task.status;
      const jobStatus = data.job && data.job.status;
      const jobRunning = jobStatus && !TERMINAL.has(jobStatus) &&
        !["DIRECT_REPLY", "CLARIFY"].includes(jobStatus);
      const jobEnded = jobStatus && TERMINAL.has(jobStatus);
      if (!selecting && (jobEnded || (TERMINAL.has(status) && !jobRunning))) {
        els.status.textContent += " · 已结束";
        clearInterval(timer);
        timer = 0;
      }
    } catch (error) {
      els.status.textContent = `读取失败：${error.message}`;
    }
  }

  if (!taskId) {
    els.status.textContent = "缺少 task_id";
    els.timeline.appendChild(el("div", "empty", "URL 里没有 task_id。"));
    return;
  }
  timer = setInterval(load, 2000);
  load();
})();
