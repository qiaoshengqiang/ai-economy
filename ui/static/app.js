/* AIEOS UI —— 无构建、无依赖 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    frontBadge: $("front-badge"),
    workModeBadge: $("work-mode-badge"),
    frontTiming: $("front-timing"),
    frontTimingText: document.querySelector("#front-timing .timing-text"),
    runningBadge: $("running-badge"),
    messages: $("messages"),
    prompt: $("prompt"),
    submit: $("submit-btn"),
    mic: $("mic-btn"),
    workspaceChip: $("workspace-chip"),
    workspaceName: $("workspace-name"),
    workspaceMenu: $("workspace-menu"),
    workspacePath: $("workspace-path"),
    workspaceChange: $("workspace-change"),
    workspaceUseTask: $("workspace-use-task"),
    repoPickerModal: $("repo-picker-modal"),
    repoPickerTitle: $("repo-picker-title"),
    repoPickerPath: $("repo-picker-path"),
    repoPickerGo: $("repo-picker-go"),
    repoPickerUp: $("repo-picker-up"),
    repoPickerList: $("repo-picker-list"),
    repoPickerStatus: $("repo-picker-status"),
    repoPickerChoose: $("repo-picker-choose"),
    repoPickerCancel: $("repo-picker-cancel"),
    repoPickerClose: $("repo-picker-close"),
    toast: $("toast"),
    agents: $("agents"),
    agentsCount: $("agents-count"),
    tasks: $("tasks"),
    tasksCount: $("tasks-count"),
    questionModal: $("question-modal"),
    questionAgent: $("question-agent"),
    questionText: $("question-text"),
    questionOptions: $("question-options"),
    questionAnswer: $("question-answer"),
    questionSubmit: $("question-submit"),
    questionClose: $("question-close"),
    questionLater: $("question-later"),
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
  let activeQuestion = null;
  let lastQuestionKey = "";
  let audioContext = null;
  let pendingFront = null;
  let timingTimer = 0;
  let timingDoneTimer = 0;
  let workdirPath = "";
  let repoPickerCurrent = "";
  let repoPickerParent = "";

  const SpeechRecognition = (
    window.SpeechRecognition || window.webkitSpeechRecognition || null);
  // Windows 暂不实现；语音输入当前只在 macOS 上验证。
  const MIC_PLATFORM_SUPPORTED = !/Windows/i.test(
    navigator.userAgent || navigator.platform || "");
  let recognition = null;
  let micActive = false;
  let micDiscardResults = false;
  let micPrefix = "";
  let micSuffix = "";
  let micFinal = "";

  function ensureAudio() {
    if (audioContext) return audioContext;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try { audioContext = new Ctx(); } catch (e) { audioContext = null; }
    return audioContext;
  }

  document.addEventListener("pointerdown", () => {
    const ctx = ensureAudio();
    if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
  }, { once: true });

  function playQuestionSound() {
    const ctx = ensureAudio();
    if (!ctx) return;
    try {
      const now = ctx.currentTime;
      [880, 1174].forEach((freq, index) => {
        const oscillator = ctx.createOscillator();
        const gain = ctx.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, now + index * 0.16);
        gain.gain.exponentialRampToValueAtTime(0.12, now + index * 0.16 + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.16 + 0.14);
        oscillator.connect(gain);
        gain.connect(ctx.destination);
        oscillator.start(now + index * 0.16);
        oscillator.stop(now + index * 0.16 + 0.16);
      });
    } catch (error) {
      // 浏览器拦截声音时忽略；气泡仍然可见。
    }
  }

  function maybeQuestionSound(agents) {
    const question = (agents || []).find((agent) => agent.question);
    const key = question ? `${question.question.task_id}:${question.question.asked_at || ""}:${question.question.question || ""}` : "";
    if (key && key !== lastQuestionKey) playQuestionSound();
    lastQuestionKey = key;
  }

  function openQuestion(question) {
    activeQuestion = question;
    els.questionAgent.textContent = question.task_id || "";
    els.questionText.textContent = question.question || "";
    els.questionOptions.innerHTML = "";
    (question.options || []).forEach((label, index) => {
      const row = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "question-option";
      radio.value = String(label);
      radio.id = `question-option-${index}`;
      row.appendChild(radio);
      row.appendChild(document.createTextNode(label));
      els.questionOptions.appendChild(row);
    });
    els.questionAnswer.value = "";
    els.questionAnswer.disabled = question.allow_free_text === false;
    if (question.allow_free_text === false) {
      els.questionAnswer.placeholder = "请选择一个选项";
    } else {
      els.questionAnswer.placeholder = "输入回答，或选择上面的选项";
    }
    els.questionModal.hidden = false;
  }

  function closeQuestion() {
    els.questionModal.hidden = true;
    activeQuestion = null;
  }

  async function submitQuestion() {
    if (!activeQuestion) return;
    const selected = document.querySelector(
      'input[name="question-option"]:checked');
    const answer = els.questionAnswer.value.trim();
    const optionId = selected ? selected.value : "";
    if (!optionId && !answer) {
      showToast("请选择一个选项或输入回答", true);
      return;
    }
    els.questionSubmit.disabled = true;
    try {
      await api("/api/answer", {
        method: "POST",
        body: JSON.stringify({
          task_id: activeQuestion.task_id,
          answer,
          option_id: optionId,
        }),
      });
      closeQuestion();
      showToast("回答已提交，Agent 将继续");
      await refresh();
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    } finally {
      els.questionSubmit.disabled = false;
    }
  }

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

  function lastFrontDuration() {
    const messages = (state && state.messages) || [];
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message.role === "front" && Number(message.duration_ms) > 0) {
        return Number(message.duration_ms);
      }
    }
    return 0;
  }

  function setFrontTimingIdle() {
    if (!els.frontTiming || pendingFront) return;
    const duration = lastFrontDuration();
    els.frontTiming.className = "front-timing idle";
    els.frontTimingText.textContent = duration
      ? `上次回复 ${formatDuration(duration)}`
      : "就绪";
  }

  function beginFrontTiming(startedAt) {
    if (!els.frontTiming) return;
    window.clearInterval(timingTimer);
    window.clearTimeout(timingDoneTimer);
    pendingFront = { jobId: "", startedAt };
    els.frontTiming.className = "front-timing waiting";
    const tick = () => {
      if (!pendingFront) return;
      const seconds = (performance.now() - startedAt) / 1000;
      els.frontTimingText.textContent = `Front 正在思考 · ${seconds.toFixed(1)}s`;
    };
    tick();
    timingTimer = window.setInterval(tick, 100);
  }

  function finishFrontTiming(milliseconds) {
    if (!pendingFront) return;
    window.clearInterval(timingTimer);
    const duration = Number(milliseconds) > 0
      ? Number(milliseconds)
      : (performance.now() - pendingFront.startedAt);
    pendingFront = null;
    els.frontTiming.className = "front-timing done";
    els.frontTimingText.textContent = `已回复 · ${formatDuration(duration) || "0.0s"}`;
    timingDoneTimer = window.setTimeout(setFrontTimingIdle, 2400);
  }

  function failFrontTiming() {
    window.clearInterval(timingTimer);
    window.clearTimeout(timingDoneTimer);
    pendingFront = null;
    els.frontTiming.className = "front-timing idle";
    els.frontTimingText.textContent = "发送失败";
    timingDoneTimer = window.setTimeout(setFrontTimingIdle, 2400);
  }

  function checkPendingFront() {
    if (!pendingFront || !pendingFront.jobId || !state) return;
    const message = (state.messages || []).find(
      (item) => item.role === "front" && item.job_id === pendingFront.jobId,
    );
    if (!message) return;
    const elapsed = performance.now() - pendingFront.startedAt;
    finishFrontTiming(Number(message.duration_ms) || elapsed);
  }

  function showToast(text, isError = false) {
    els.toast.textContent = String(text || "");
    els.toast.classList.toggle("error", !!isError);
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { els.toast.hidden = true; }, 6500);
  }

  function setMicState(active) {
    micActive = !!active;
    if (!els.mic) return;
    els.mic.classList.toggle("recording", micActive);
    els.mic.setAttribute("aria-pressed", micActive ? "true" : "false");
    els.mic.title = micActive ? "停止语音输入" : "语音输入（macOS）";
  }

  function renderMicText(interim) {
    const text = `${micFinal}${interim || ""}`;
    els.prompt.value = `${micPrefix}${text}${micSuffix}`;
    const cursor = (micPrefix + text).length;
    els.prompt.setSelectionRange(cursor, cursor);
  }

  function startMic() {
    if (!recognition || micActive) return;
    const focused = document.activeElement === els.prompt;
    const value = els.prompt.value;
    const start = focused ? els.prompt.selectionStart : value.length;
    const end = focused ? els.prompt.selectionEnd : value.length;
    micPrefix = value.slice(0, Math.min(start, end));
    micSuffix = value.slice(Math.max(start, end));
    micFinal = "";
    micDiscardResults = false;
    setMicState(true);
    try {
      recognition.start();
    } catch (error) {
      setMicState(false);
      if (!error || error.name !== "InvalidStateError") {
        showToast(
          `语音输入启动失败：${error && error.message ? error.message : error}`,
          true,
        );
      }
    }
  }

  function stopMic({ discard = false } = {}) {
    if (!recognition) return;
    micDiscardResults = !!discard;
    if (micActive) {
      try {
        if (discard) recognition.abort();
        else recognition.stop();
      } catch (_error) { /* already stopped */ }
    }
    setMicState(false);
    if (discard) {
      micPrefix = "";
      micSuffix = "";
      micFinal = "";
    }
  }

  function setupMic() {
    if (!els.mic) return;
    // 浏览器不支持 / Windows 暂不实现时直接隐藏，不打扰用户。
    if (!SpeechRecognition || !MIC_PLATFORM_SUPPORTED) {
      els.mic.hidden = true;
      return;
    }
    recognition = new SpeechRecognition();
    recognition.lang = "zh-CN";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => setMicState(true);
    recognition.onresult = (event) => {
      // 提交后浏览器可能还会补发最后一次 result；不能再写回已清空的输入框。
      if (micDiscardResults) return;
      let interim = "";
      for (
        let index = event.resultIndex;
        index < event.results.length;
        index += 1
      ) {
        const result = event.results[index];
        if (!result || !result[0]) continue;
        if (result.isFinal) micFinal += result[0].transcript;
        else interim += result[0].transcript;
      }
      renderMicText(interim);
    };
    recognition.onerror = (event) => {
      const code = String((event && event.error) || "");
      if (code === "aborted") return;
      setMicState(false);
      const messages = {
        "not-allowed": "麦克风权限被拒绝，请在浏览器和系统设置中允许",
        "service-not-allowed": "语音识别服务不可用",
        "audio-capture": "没有找到可用的麦克风",
        "no-speech": "没有听到语音，请再试一次",
        "network": "语音识别服务连接失败；Chrome 需要网络",
      };
      showToast(
        messages[code] || `语音输入失败：${code || "未知错误"}`,
        true,
      );
    };
    recognition.onend = () => setMicState(false);

    els.mic.hidden = false;
    els.mic.addEventListener("click", () => {
      if (micActive) stopMic();
      else startMic();
    });
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

  function renderWorkspace(path, info = null) {
    const value = String(path || "").trim();
    if (!value) {
      els.workspaceName.textContent = "Task Workspace";
      els.workspacePath.textContent = "AIEOS Task Workspace";
      els.workspaceChip.title = "AIEOS Task Workspace";
      els.workspaceChip.classList.remove("error");
      return;
    }
    const parts = value.replace(/\/+$/, "").split("/");
    els.workspaceName.textContent = parts[parts.length - 1] || value;
    els.workspacePath.textContent = info && !info.is_dir
      ? `${value}\n${info.error || "目录不可用"}`
      : value;
    els.workspaceChip.title = value;
    els.workspaceChip.classList.toggle(
      "error", !!(info && !info.is_dir));
  }

  function setWorkdir(path, info = null) {
    workdirPath = String(path || "").trim();
    if (workdirPath) localStorage.setItem("aieos.workdir", workdirPath);
    else localStorage.removeItem("aieos.workdir");
    renderWorkspace(workdirPath, info);
  }

  async function validateWorkdir(path) {
    const value = String(path || "").trim();
    if (!value) {
      setWorkdir("");
      return { path: "", is_dir: true, error: "" };
    }
    try {
      const info = await api(
        `/api/workdir?path=${encodeURIComponent(value)}`);
      setWorkdir(value, info);
      return info;
    } catch (error) {
      renderWorkspace(value, { is_dir: false, error: error.message });
      return null;
    }
  }

  function closeRepoPicker() {
    els.repoPickerModal.hidden = true;
  }

  async function loadRepoPicker(path) {
    try {
      const data = await api(
        `/api/fs/dirs?path=${encodeURIComponent(path || "")}`);
      repoPickerCurrent = data.path || "";
      repoPickerParent = data.parent || "";
      els.repoPickerPath.value = repoPickerCurrent;
      els.repoPickerUp.disabled = !repoPickerParent;
      els.repoPickerStatus.textContent = "选择 Agent 要直接修改的项目目录";
      els.repoPickerStatus.classList.add("ok");
      els.repoPickerList.innerHTML = "";
      if (!data.entries || !data.entries.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "没有子目录";
        els.repoPickerList.appendChild(empty);
      } else {
        data.entries.forEach((entry) => {
          const row = document.createElement("div");
          row.className = "repo-dir-row";
          row.title = entry.path;
          const name = document.createElement("span");
          name.className = "repo-dir-name";
          name.textContent = entry.name;
          row.appendChild(name);
          if (entry.is_git_repo) {
            const tag = document.createElement("span");
            tag.className = "repo-dir-tag";
            tag.textContent = "Git 仓库";
            row.appendChild(tag);
          }
          row.addEventListener("click", () => loadRepoPicker(entry.path));
          els.repoPickerList.appendChild(row);
        });
      }
    } catch (error) {
      els.repoPickerStatus.textContent = `读取目录失败：${error.message}`;
      els.repoPickerStatus.classList.remove("ok");
    }
  }

  async function openRepoPicker() {
    els.repoPickerTitle.textContent = "选择工作目录（已有项目）";
    els.repoPickerModal.hidden = false;
    await loadRepoPicker(workdirPath);
  }

  async function chooseRepoPicker() {
    const info = await validateWorkdir(repoPickerCurrent);
    if (!info || !info.is_dir) {
      els.repoPickerStatus.textContent = "请选择存在的目录";
      els.repoPickerStatus.classList.remove("ok");
      return;
    }
    setWorkdir(info.path, info);
    closeRepoPicker();
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

  function robotMarkup(working, front) {
    const classes = [
      "robot",
      working ? "working" : "idle",
      front ? "front" : "",
    ].filter(Boolean).join(" ");
    return `<svg class="${classes}" viewBox="0 0 24 24" aria-hidden="true">
      <g class="antenna">
        <path d="M12 5.2V3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
        <circle class="antenna-tip" cx="12" cy="2.1" r="1.2" fill="currentColor"/>
      </g>
      <g class="body">
        <rect x="5" y="5" width="14" height="12" rx="3.2"
          fill="rgba(94,224,255,.06)" stroke="currentColor" stroke-width="1.2"/>
        <rect x="7.5" y="7.4" width="9" height="6.4" rx="1.8"
          fill="rgba(8,11,18,.6)" stroke="currentColor" stroke-width=".8"
          stroke-opacity=".45"/>
        <circle class="eye" cx="9.9" cy="10.4" r=".95" fill="currentColor"/>
        <circle class="eye" cx="14.1" cy="10.4" r=".95" fill="currentColor"/>
        <path d="M10.2 13.2h3.6" stroke="currentColor" stroke-width=".9"
          stroke-linecap="round" stroke-opacity=".55"/>
      </g>
      <g class="arm arm-l">
        <path d="M5 10.2v2.1" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </g>
      <g class="arm arm-r">
        <path d="M19 10.2v2.1" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </g>
      <g class="legs">
        <path d="M9 17v2.1M15 17v2.1" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>
      </g>
    </svg>`;
  }

  function renderAgents(agents, tasks) {
    const list = agents || [];
    const settings = (state && state.settings) || {};
    const frontMode = settings.front_mode === "relay" ? "只传达" : "自主";
    const workMode = settings.work_mode === "autonomous" ? "自主" : "工作流";
    const taskMap = new Map((tasks || []).map((task) => [task.task_id, task]));
    const key = JSON.stringify({
      agents: list,
      front_mode: settings.front_mode || "autonomous",
      work_mode: settings.work_mode || "workflow",
      tasks: (tasks || []).map((task) => [task.task_id, task.description]),
    });
    if (key === lastAgentsKey && els.agents.childElementCount) return;
    lastAgentsKey = key;
    els.agentsCount.textContent = `${list.length} 个`;
    els.agents.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "还没有 Agent。";
      els.agents.appendChild(empty);
      return;
    }
    list.forEach((agent) => {
      const isFront = agent.agent_id === state.front_agent
        || agent.role === "FRONT";
      const working = agent.status === "WORKING"
        || agent.status === "STOPPING";
      const row = document.createElement("div");
      row.className = working ? "agent-row working" : "agent-row";
      if (working) {
        row.insertAdjacentHTML("afterbegin", `
          <svg class="agent-border" aria-hidden="true">
            <rect x="0" y="0" width="100%" height="100%"></rect>
          </svg>`);
      }

      const avatar = document.createElement("span");
      avatar.className = "agent-avatar";
      avatar.innerHTML = robotMarkup(working, isFront);
      const dot = document.createElement("span");
      dot.className = `dot ${agent.status || ""}`;
      avatar.appendChild(dot);
      row.appendChild(avatar);

      const id = document.createElement("span");
      id.className = "agent-id";
      id.textContent = agent.agent_id;
      row.appendChild(id);

      const engine = document.createElement("span");
      engine.className = "agent-engine";
      const isPi = (settings.pi_work_agents || []).includes(agent.agent_id);
      engine.textContent = isFront ? "FRONT" : (isPi ? "PI" : "SOLO");
      row.appendChild(engine);

      const mode = document.createElement("span");
      mode.className = "agent-mode";
      const tag = document.createElement("span");
      if (isFront) {
        tag.className = "tag front";
        tag.textContent = `Front · ${frontMode}`;
      } else {
        tag.className = `tag ${workMode === "自主" ? "auto" : "flow"}`;
        tag.textContent = `Work · ${workMode}`;
      }
      mode.appendChild(tag);
      row.appendChild(mode);

      const task = document.createElement("span");
      task.className = "agent-task";
      if (agent.task_id) {
        const ref = taskMap.get(agent.task_id);
        const desc = ref && ref.description
          ? ` · ${ref.description.slice(0, 26)}`
          : "";
        task.textContent = `${agent.task_id}${desc}`;
        task.title = ref && ref.description ? ref.description : agent.task_id;
      } else {
        task.textContent = agent.has_brain ? "IDLE · ready" : "no brain";
      }
      row.appendChild(task);

      const levels = Array.isArray(agent.thinking_levels)
        ? agent.thinking_levels : [];
      if (levels.length) {
        const thinking = document.createElement("span");
        thinking.className = "agent-thinking";
        const label = document.createElement("span");
        label.textContent = "思考";
        const select = document.createElement("select");
        select.className = "agent-thinking-select";
        select.title = isFront
          ? "Front 决策思考强度；Auto 默认 low"
          : "Prototype 固定 max；这里设置 Engineering / Evolution";
        const auto = document.createElement("option");
        auto.value = "auto";
        auto.textContent = isFront ? "Auto（low）" : "Auto";
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
        thinking.appendChild(label);
        thinking.appendChild(select);
        row.appendChild(thinking);
      }

      if (agent.question) {
        const bubble = document.createElement("button");
        bubble.type = "button";
        bubble.className = "agent-question-bubble";
        bubble.textContent = "?";
        bubble.title = "Agent 需要你的确认，点击回答";
        bubble.setAttribute("aria-label", "回答 Agent 的问题");
        bubble.addEventListener("click", (event) => {
          event.stopPropagation();
          openQuestion({ ...agent.question, task_id: agent.question.task_id || agent.task_id });
        });
        row.appendChild(bubble);
      }
      els.agents.appendChild(row);
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
      card.title = "在新任务页打开执行明细";
      card.addEventListener("click", () => {
        if (hasActiveTextSelection()) return;
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
      card.appendChild(top);

      const modes = document.createElement("div");
      modes.className = "task-modes";
      const front = document.createElement("span");
      front.className = "tag front";
      front.textContent = task.front_mode === "relay"
        ? "Front 只传达"
        : (task.front_mode === "autonomous" ? "Front 自主" : "Front —");
      modes.appendChild(front);
      const work = document.createElement("span");
      work.className = task.work_mode === "autonomous"
        ? "tag auto"
        : (task.work_mode === "workflow" ? "tag flow" : "tag");
      work.textContent = task.work_mode === "autonomous"
        ? "Work 自主"
        : (task.work_mode === "workflow" ? "Work 工作流" : "Work 默认");
      modes.appendChild(work);
      const iteration = document.createElement("span");
      iteration.className = "tag";
      iteration.textContent = `迭代 ${task.iteration || 1}`;
      modes.appendChild(iteration);
      if (task.workspace_mode === "project") {
        const project = document.createElement("span");
        project.className = "tag";
        project.textContent = "项目目录";
        project.title = task.project_path || "";
        modes.appendChild(project);
      }
      card.appendChild(modes);

      const desc = document.createElement("div");
      desc.className = "task-desc";
      desc.textContent = task.description || "";
      card.appendChild(desc);

      const meta = document.createElement("div");
      meta.className = "task-meta";
      const leader = document.createElement("span");
      leader.textContent = `Agent ${task.leader || "—"}`;
      meta.appendChild(leader);
      const others = (task.members || []).filter(
        (member) => member !== task.leader,
      );
      if (others.length) {
        const members = document.createElement("span");
        members.textContent = `成员 ${others.join(", ")}`;
        meta.appendChild(members);
      }
      card.appendChild(meta);
      els.tasks.appendChild(card);
    });
  }

  function render() {
    if (!state) return;
    const settings = state.settings || {};
    const frontMode = settings.front_mode === "relay" ? "只传达" : "自主";
    const workAuto = settings.work_mode !== "workflow";
    els.frontBadge.textContent = `Front: ${state.front_agent || "—"} · ${frontMode}`;
    els.frontBadge.classList.toggle("front-relay", frontMode === "只传达");
    els.workModeBadge.textContent = `Work: ${workAuto ? "自主" : "工作流"}`;
    els.workModeBadge.classList.toggle("work-auto", workAuto);
    els.workModeBadge.classList.toggle("work-flow", !workAuto);
    els.runningBadge.textContent = `Running: ${state.running_jobs || 0}`;
    checkPendingFront();
    if (!pendingFront && els.frontTiming.classList.contains("idle")) {
      setFrontTimingIdle();
    }
    // 有选区时冻结列表重绘；用户点掉选区后，下一次轮询会补上最新数据。
    if (hasActiveTextSelection()) return;
    maybeQuestionSound(state.agents || []);
    renderMessages(state.messages || []);
    renderAgents(state.agents, state.tasks);
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
    if (recognition) stopMic({ discard: true });
    const text = els.prompt.value.trim();
    if (!text) {
      showToast("请先输入内容");
      return;
    }
    submitting = true;
    els.submit.disabled = true;
    const startedAt = performance.now();
    beginFrontTiming(startedAt);
    try {
      let workdir = workdirPath;
      if (workdir) {
        const info = await validateWorkdir(workdir);
        if (!info || !info.is_dir) {
          throw new Error("工作目录不存在或不是目录");
        }
        workdir = info.path;
      }
      const submitted = await api("/api/submit", {
        method: "POST",
        body: JSON.stringify({ text, workdir }),
      });
      pendingFront = {
        jobId: String(submitted.job_id || ""),
        startedAt,
      };
      els.prompt.value = "";
      await refresh();
    } catch (error) {
      failFrontTiming();
      showToast(`提交失败：${error.message}`, true);
    } finally {
      submitting = false;
      els.submit.disabled = false;
      els.prompt.focus();
    }
  }

  /* events */
  els.submit.addEventListener("click", submitPrompt);
  els.workspaceChip.addEventListener("click", (event) => {
    event.stopPropagation();
    els.workspaceMenu.hidden = !els.workspaceMenu.hidden;
  });
  els.workspaceChange.addEventListener("click", () => {
    els.workspaceMenu.hidden = true;
    openRepoPicker();
  });
  els.workspaceUseTask.addEventListener("click", () => {
    els.workspaceMenu.hidden = true;
    setWorkdir("");
  });
  document.addEventListener("click", (event) => {
    if (
      !els.workspaceMenu.hidden
      && !els.workspaceMenu.contains(event.target)
      && !els.workspaceChip.contains(event.target)
    ) {
      els.workspaceMenu.hidden = true;
    }
  });
  els.repoPickerGo.addEventListener("click", () => {
    loadRepoPicker((els.repoPickerPath.value || "").trim());
  });
  els.repoPickerUp.addEventListener("click", () => {
    if (repoPickerParent) loadRepoPicker(repoPickerParent);
  });
  els.repoPickerChoose.addEventListener("click", chooseRepoPicker);
  els.repoPickerCancel.addEventListener("click", closeRepoPicker);
  els.repoPickerClose.addEventListener("click", closeRepoPicker);
  els.repoPickerModal.addEventListener("click", (event) => {
    if (event.target === els.repoPickerModal) closeRepoPicker();
  });
  els.repoPickerPath.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadRepoPicker((els.repoPickerPath.value || "").trim());
    }
  });
  els.questionSubmit.addEventListener("click", submitQuestion);
  els.questionClose.addEventListener("click", closeQuestion);
  els.questionLater.addEventListener("click", closeQuestion);
  els.questionModal.addEventListener("click", (event) => {
    if (event.target === els.questionModal) closeQuestion();
  });
  els.prompt.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      submitPrompt();
    }
  });
  window.addEventListener("beforeunload", () => {
    if (recognition && micActive) {
      try { recognition.abort(); } catch (_error) { /* ignore */ }
    }
  });
  setupMic();
  const savedWorkdir = localStorage.getItem("aieos.workdir") || "";
  if (savedWorkdir) {
    setWorkdir(savedWorkdir);
    validateWorkdir(savedWorkdir);
  }
  refresh();
  setInterval(refresh, 1200);
})();
