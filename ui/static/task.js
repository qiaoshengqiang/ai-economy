/* AIEOS Task 详情页：轮次 / 思考 / 输出 / 动作 / 结果。 */
(() => {
  "use strict";

  const taskId = new URLSearchParams(location.search).get("task_id") || "";
  const els = {
    status: document.getElementById("status"),
    meta: document.getElementById("task-meta"),
    prompt: document.getElementById("task-prompt"),
    timeline: document.getElementById("timeline"),
    workspace: document.getElementById("workspace"),
    memory: document.getElementById("public-memory"),
    llm: document.getElementById("llm"),
    executions: document.getElementById("executions"),
  };
  let timer = 0;
  let lastSignature = "";

  const TERMINAL = new Set(["COMPLETED", "CANCELLED", "FAILED"]);

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

  function metaItem(parent, key, value) {
    const item = el("div", "meta-item");
    item.appendChild(el("div", "meta-key", key));
    item.appendChild(el("div", "meta-value", value == null ? "—" : value));
    parent.appendChild(item);
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
    els.meta.appendChild(el("div", "section-title", "任务"));
    const grid = el("div", "meta-grid");
    metaItem(grid, "task_id", task.task_id || taskId);
    metaItem(grid, "status", task.status || "—");
    metaItem(grid, "mode", task.mode || "—");
    metaItem(grid, "iteration", task.iteration || 1);
    metaItem(grid, "执行 Agent", task.creator || "—");
    metaItem(grid, "contractor", task.contractor || "—");
    metaItem(grid, "participants", (task.participants || []).join(", ") || "—");
    metaItem(grid, "workspace", task.workspace_id || "—");
    metaItem(grid, "created", task.created_at || "—");
    metaItem(grid, "updated", task.updated_at || "—");
    if (job) {
      metaItem(grid, "job", `${job.status || "?"}${job.error ? " · " + job.error : ""}`);
    }
    els.meta.appendChild(grid);
  }

  function renderPrompt(data) {
    const task = data.task || {};
    els.prompt.innerHTML = "";
    els.prompt.appendChild(el("div", "section-title", "交给 Work 的提示词"));
    els.prompt.appendChild(el("div", "prompt-label", "Front → Work"));
    els.prompt.appendChild(pre(task.description || "（无）"));
  }

  function renderAction(action, result) {
    const box = el("div", "action");
    const head = el("div", "action-head");
    head.appendChild(el("span", "action-type",
      action.action_type || action.type || "?"));
    const target = targetOf(action);
    if (target) head.appendChild(el("span", "action-target", target));
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
    if (seg.tokens != null) head.appendChild(badge(`${seg.tokens} tokens`));
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
    els.timeline.innerHTML = "";
    if (!rounds.length) {
      els.timeline.appendChild(el("div", "empty",
        "还没有轮次记录。旧任务可能没有落盘 phase 明细。"));
      return;
    }
    rounds.forEach((round) => {
      const block = document.createElement("details");
      block.className = `round${round.cognition ? " cognition" : ""}`;
      block.open = true;
      const summary = document.createElement("summary");
      summary.textContent = round.cognition
        ? `任务认知 · ${round.tokens || 0} tokens`
        : `Round ${round.round || "?"} · ` +
          `${round.status_before || "?"} → ${round.status_after || "?"} · ` +
          `${round.tokens || 0} tokens`;
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
    els.llm.appendChild(el("div", "file-row",
      `${llm.calls || 0} calls · ${llm.input_tokens || 0} in · ` +
      `${llm.output_tokens || 0} out · ${llm.cost_ai || 0} AIC`));
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
    els.status.textContent =
      `${task.status || "?"}${data.job ? " · " + data.job.status : ""}`;
    renderMeta(data);
    renderPrompt(data);
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
      if (signature !== lastSignature) {
        lastSignature = signature;
        render(data);
      }
      const status = data.task && data.task.status;
      const jobStatus = data.job && data.job.status;
      const jobRunning = jobStatus && !TERMINAL.has(jobStatus) &&
        !["DIRECT_REPLY", "CLARIFY"].includes(jobStatus);
      if (TERMINAL.has(status) && !jobRunning) {
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
