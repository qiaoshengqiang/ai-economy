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
    activityBell: $("activity-bell"),
    activityBadge: $("activity-badge"),
    activityBackdrop: $("activity-backdrop"),
    activityDrawer: $("activity-drawer"),
    activityClose: $("activity-close"),
    activityMark: $("activity-mark"),
    activityList: $("activity-list"),
    activityFilters: document.querySelector(".activity-filters"),
    detailModal: $("detail-modal"),
    detailBackdrop: $("detail-backdrop"),
    detailRun: $("detail-run"),
    detailBody: $("detail-body"),
    detailClose: $("detail-close"),
    memoryBtn: $("memory-btn"),
    memoryBackdrop: $("memory-backdrop"),
    memoryModal: $("memory-modal"),
    memoryClose: $("memory-close"),
    memoryAgent: $("memory-agent"),
    memoryClear: $("memory-clear"),
    memoryStats: $("memory-stats"),
    memorySubtitle: $("memory-subtitle"),
    memoryBody: $("memory-body"),
    memoryShortCount: $("memory-short-count"),
    memoryLongCount: $("memory-long-count"),
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
    taskFilters: $("task-filters"),
    questionModal: $("question-modal"),
    questionAgent: $("question-agent"),
    questionText: $("question-text"),
    questionOptions: $("question-options"),
    questionAnswer: $("question-answer"),
    questionSubmit: $("question-submit"),
    questionClose: $("question-close"),
    questionLater: $("question-later"),
    confirmModal: $("confirm-modal"),
    confirmDialog: $("confirm-dialog"),
    confirmIcon: $("confirm-icon"),
    confirmTitle: $("confirm-title"),
    confirmMessage: $("confirm-message"),
    confirmTargetWrap: $("confirm-target-wrap"),
    confirmTarget: $("confirm-target"),
    confirmStatus: $("confirm-status"),
    confirmDetails: $("confirm-details"),
    confirmCancel: $("confirm-cancel"),
    confirmOk: $("confirm-ok"),
    confirmClose: $("confirm-close"),
    confirmFootnote: $("confirm-footnote"),
  };

  let state = null;
  let polling = false;
  let submitting = false;
  let toastTimer = 0;

  // 轮询重绘不能清掉用户正在拖选的文本。
  // pointerSelecting 覆盖“刚按下鼠标、选区还没形成”的瞬间。
  let pointerSelecting = false;
  let lastMessagesKey = "";
  let forceMessagesScroll = false;
  let lastAgentsKey = "";
  let lastTasksKey = "";
  let taskFilter = "all";
  let activeQuestion = null;
  let lastQuestionKey = "";
  let audioContext = null;
  let pendingFront = null;
  let activityOpen = false;
  let activityFilter = "all";
  let activityItems = null;
  let activityLoading = false;
  let lastActivityUnread = -1;
  let openActivityRun = "";
  let memoryOpen = false;
  let memoryAgentId = "";
  let memoryTab = "short";
  let memoryData = null;
  let memoryLoading = false;
  let memoryRequestToken = 0;
  let timingTimer = 0;
  let timingDoneTimer = 0;
  let workdirPath = "";
  let repoPickerCurrent = "";
  let repoPickerParent = "";
  const PENDING_SUBMIT_KEY = "aieos.pendingSubmit";
  let pendingSubmission = null;

  // 语音输入先在前端隐藏；代码保留，后续把开关打开即可恢复。
  const VOICE_INPUT_ENABLED = false;
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

  /* 通用确认弹窗：替代浏览器原生 confirm()，返回 Promise<boolean>。 */
  const CONFIRM_TONES = ["danger", "warning", "default"];
  let confirmResolver = null;
  let confirmReturnFocus = null;

  function finishConfirm(result) {
    if (!confirmResolver) return;
    const resolve = confirmResolver;
    confirmResolver = null;
    els.confirmModal.hidden = true;
    els.confirmDialog.className = "modal confirm-modal";
    if (confirmReturnFocus && typeof confirmReturnFocus.focus === "function") {
      try { confirmReturnFocus.focus(); } catch (_error) { /* ignore */ }
    }
    confirmReturnFocus = null;
    resolve(result);
  }

  function confirmDialog(options) {
    const opts = options || {};
    const tone = CONFIRM_TONES.includes(opts.tone) ? opts.tone : "default";
    if (confirmResolver) finishConfirm(false);
    const target = String(opts.target || "").trim();
    const status = String(opts.status || "").trim();
    const details = Array.isArray(opts.details) ? opts.details : [];
    confirmReturnFocus = document.activeElement;
    els.confirmDialog.className = `modal confirm-modal ${tone}`;
    els.confirmIcon.textContent = tone === "default" ? "?" : "!";
    els.confirmTitle.textContent = opts.title || "确认操作？";
    els.confirmMessage.textContent = opts.message || "";
    els.confirmMessage.hidden = !opts.message;
    els.confirmTargetWrap.hidden = !target && !status;
    els.confirmTarget.textContent = target;
    els.confirmTarget.hidden = !target;
    els.confirmStatus.hidden = !status;
    els.confirmStatus.className = `task-status ${status}`;
    els.confirmStatus.textContent = status;
    els.confirmDetails.innerHTML = "";
    if (details.length) {
      if (opts.detailsTitle) {
        const head = document.createElement("b");
        head.textContent = opts.detailsTitle;
        els.confirmDetails.appendChild(head);
      }
      const list = document.createElement("ul");
      details.forEach((detail) => {
        const item = document.createElement("li");
        item.textContent = String(detail);
        list.appendChild(item);
      });
      els.confirmDetails.appendChild(list);
    }
    els.confirmDetails.hidden = !details.length;
    els.confirmOk.textContent = opts.confirmText || "确认";
    els.confirmOk.className = `cd-btn ${
      tone === "danger" ? "danger"
        : (tone === "warning" ? "warning" : "primary")}`;
    els.confirmCancel.textContent = opts.cancelText || "取消";
    els.confirmFootnote.textContent = (
      opts.footnote || "Esc 取消 · 默认焦点在“取消”");
    els.confirmModal.hidden = false;
    els.confirmCancel.focus();
    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  }

  // 设置页等模块复用同一个确认弹窗。
  window.aieosConfirm = confirmDialog;

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
    if (
      !VOICE_INPUT_ENABLED
      || !SpeechRecognition
      || !MIC_PLATFORM_SUPPORTED
    ) {
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

  function scrollMessagesToBottom() {
    const box = els.messages;
    if (!box) return;
    box.scrollTop = box.scrollHeight;
  }

  function renderMessages(messages) {
    const box = els.messages;
    const key = JSON.stringify(messages || []);
    const forceScroll = forceMessagesScroll;
    forceMessagesScroll = false;
    if (key === lastMessagesKey && box.childElementCount) {
      if (forceScroll) scrollMessagesToBottom();
      return;
    }
    lastMessagesKey = key;
    const atBottom = forceScroll
      || box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
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
      const metaLabel = message.role === "task" ? "Task log"
        : (message.role === "system" ? "系统" : "");
      const metaParts = [];
      if (metaLabel) metaParts.push(metaLabel);
      if (message.task_id) metaParts.push(message.task_id);
      if (metaParts.length) {
        const meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = metaParts.join(" · ");
        item.appendChild(meta);
      }
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
    if (atBottom) scrollMessagesToBottom();
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
    const list = (agents || []).filter((agent) => !agent.archived);
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
      id.textContent = agent.name || agent.agent_id;
      id.title = agent.agent_id;
      row.appendChild(id);

      const engine = document.createElement("span");
      engine.className = "agent-engine";
      const isPi = (settings.pi_work_agents || []).includes(agent.agent_id);
      engine.textContent = isFront ? "FRONT" : (isPi ? "PI" : "BUILTIN");
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

  const TASK_TERMINAL = new Set([
    "COMPLETED", "CANCELLED", "REJECTED", "EXPIRED", "FAILED",
  ]);

  function closeTaskMenus() {
    document.querySelectorAll(".task-card-menu").forEach((menu) => {
      menu.hidden = true;
    });
    document.querySelectorAll(".task-more").forEach((button) => {
      button.setAttribute("aria-expanded", "false");
    });
  }

  async function taskAction(action, task) {
    const taskId = String(
      task && task.task_id ? task.task_id : (task || "")).trim();
    if (!taskId) return;
    if (action === "delete") {
      const ok = await confirmDialog({
        tone: "danger",
        title: "删除任务？",
        message: "删除后 Task 记录和任务数据会永久清理，无法恢复。如果只是不想在列表中看到它，可以改用“归档”。",
        target: taskId,
        status: task && task.status ? task.status : "",
        detailsTitle: "将同时清理：",
        details: [
          "Task 记录、执行历史与 Task Shared Context",
          "Public / Private Working Memory 与提交请求",
          "tasks/<task_id> 目录、快照与工作区文件",
        ],
        confirmText: "删除任务",
        cancelText: "取消",
      });
      if (!ok) return;
    }
    const endpoint = {
      archive: "/api/task/archive",
      unarchive: "/api/task/unarchive",
      delete: "/api/task/delete",
    }[action];
    if (!endpoint) return;
    try {
      await api(endpoint, {
        method: "POST",
        body: JSON.stringify({ task_id: taskId }),
      });
      showToast(
        action === "delete" ? "任务已删除"
          : (action === "archive" ? "任务已归档" : "任务已恢复"),
      );
      lastTasksKey = "";
      await refresh();
    } catch (error) {
      showToast(`操作失败：${error.message}`, true);
    }
  }

  function renderTasks(tasks) {
    const all = tasks || [];
    const visible = all.filter((task) => {
      if (taskFilter === "archived") return !!task.archived;
      if (task.archived) return false;
      if (taskFilter === "completed") return task.status === "COMPLETED";
      if (taskFilter === "active") {
        return !TASK_TERMINAL.has(String(task.status || ""));
      }
      return true;
    });
    const key = JSON.stringify({ filter: taskFilter, tasks: visible });
    if (key === lastTasksKey && els.tasks.childElementCount) return;
    lastTasksKey = key;
    els.tasksCount.textContent = `${visible.length} 个`;
    els.tasks.innerHTML = "";
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = taskFilter === "archived"
        ? "没有已归档的 Task。"
        : "还没有 Task。";
      els.tasks.appendChild(empty);
      return;
    }
    visible.forEach((task) => {
      const status = String(task.status || "");
      const archived = !!task.archived;
      const live = !!task.live;
      const card = document.createElement("div");
      card.className = (
        `task-card clickable ${status}${archived ? " archived" : ""}`
      );
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
      top.appendChild(id);

      const headRight = document.createElement("span");
      headRight.className = "task-head-right";
      const statusNode = document.createElement("span");
      statusNode.className = `task-status ${status}`;
      statusNode.textContent = status || "—";
      headRight.appendChild(statusNode);
      if (archived) {
        const archivedBadge = document.createElement("span");
        archivedBadge.className = "archived-badge";
        archivedBadge.textContent = "已归档";
        headRight.appendChild(archivedBadge);
      }
      const more = document.createElement("button");
      more.type = "button";
      more.className = "task-more";
      more.textContent = "⋯";
      more.title = "更多操作";
      more.setAttribute("aria-expanded", "false");
      headRight.appendChild(more);

      const menu = document.createElement("div");
      menu.className = "task-card-menu";
      menu.hidden = true;
      const archiveButton = document.createElement("button");
      archiveButton.type = "button";
      archiveButton.textContent = archived ? "▣ 恢复任务" : "▣ 归档任务";
      archiveButton.disabled = !archived && live;
      archiveButton.addEventListener("click", (event) => {
        event.stopPropagation();
        closeTaskMenus();
        taskAction(archived ? "unarchive" : "archive", task);
      });
      menu.appendChild(archiveButton);

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "danger";
      deleteButton.textContent = "⌫ 删除任务";
      deleteButton.disabled = live || !TASK_TERMINAL.has(status);
      deleteButton.addEventListener("click", (event) => {
        event.stopPropagation();
        closeTaskMenus();
        taskAction("delete", task);
      });
      menu.appendChild(deleteButton);

      const note = document.createElement("div");
      note.className = "menu-note";
      note.textContent = live
        ? "任务运行中，请先停止任务。"
        : (!TASK_TERMINAL.has(status)
          ? "只有已结束的任务可以删除。"
          : "删除不可恢复；归档可以恢复。");
      menu.appendChild(note);

      more.addEventListener("click", (event) => {
        event.stopPropagation();
        const shouldOpen = menu.hidden;
        closeTaskMenus();
        menu.hidden = !shouldOpen;
        more.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
      });
      menu.addEventListener("click", (event) => event.stopPropagation());
      headRight.appendChild(menu);
      top.appendChild(headRight);
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

  /* ---------------- Activity（唤醒记录，不是聊天消息） ---------------- */
  const ACTIVITY_OUTCOMES = {
    replied: { cls: "notified", label: "已通知", group: "notified" },
    dispatched: { cls: "notified", label: "已派发", group: "notified" },
    no_notify: { cls: "silent", label: "未通知", group: "silent" },
    failed: { cls: "failed", label: "异常", group: "failed" },
    rejected: { cls: "failed", label: "已拒绝", group: "failed" },
    guard_limit: { cls: "failed", label: "已暂停", group: "failed" },
    skipped_busy: { cls: "silent", label: "跳过", group: "silent" },
    running: { cls: "silent", label: "进行中", group: "silent" },
  };

  function activityOutcome(run) {
    const key = String((run && run.outcome) || "");
    return ACTIVITY_OUTCOMES[key] || {
      cls: "failed",
      label: key || "未知",
      group: "failed",
    };
  }

  function activityKind(kind) {
    const value = String(kind || "");
    if (value === "alarm") return "定时检查";
    return value || "唤醒";
  }

  function activityTime(value) {
    const text = String(value || "");
    if (!text) return "—";
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    const pad = (number) => String(number).padStart(2, "0");
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
      + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function setActivityOpen(open) {
    activityOpen = Boolean(open);
    els.activityDrawer.classList.toggle("open", activityOpen);
    els.activityBackdrop.classList.toggle("open", activityOpen);
    els.activityBell.setAttribute("aria-expanded", String(activityOpen));
    if (activityOpen) {
      renderActivities();
      loadActivities();
    }
  }

  async function loadActivities() {
    if (activityLoading) return;
    activityLoading = true;
    try {
      const data = await api("/api/activities?limit=50");
      activityItems = data.activities || [];
      renderActivities();
    } catch (error) {
      showToast(`活动记录读取失败：${error.message}`, true);
    } finally {
      activityLoading = false;
    }
  }

  function renderActivities() {
    const box = els.activityList;
    if (!box) return;
    box.textContent = "";
    const items = (activityItems || []).filter((run) => (
      activityFilter === "all"
      || activityOutcome(run).group === activityFilter
    ));
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "activity-empty";
      const strong = document.createElement("strong");
      strong.textContent = activityItems === null
        ? "正在读取…"
        : "没有符合条件的记录";
      empty.appendChild(strong);
      box.appendChild(empty);
      return;
    }
    items.forEach((run) => box.appendChild(renderActivityRow(run)));
  }

  function renderActivityRow(run) {
    const row = document.createElement("div");
    row.className = "activity-row" + (run.unread ? " unread" : "");
    row.tabIndex = 0;
    row.setAttribute("role", "button");

    const top = document.createElement("div");
    top.className = "activity-row-top";
    const time = document.createElement("span");
    time.className = "activity-time";
    time.textContent = activityTime(run.started_at);
    const outcome = activityOutcome(run);
    const badge = document.createElement("span");
    badge.className = "activity-outcome " + outcome.cls;
    badge.textContent = outcome.label;
    top.append(time, badge);

    const title = document.createElement("p");
    title.className = "activity-title";
    const kind = document.createElement("span");
    kind.className = "activity-kind";
    kind.textContent = activityKind(run.kind);
    const text = document.createElement("span");
    text.className = "activity-text";
    text.textContent = String(run.summary || "（没有摘要）");
    title.append(kind, text);

    row.append(top, title);

    const tasks = (run.task_ids || []).filter(Boolean);
    if (tasks.length) {
      const chips = document.createElement("div");
      chips.className = "activity-tasks";
      tasks.forEach((task) => {
        const chip = document.createElement("span");
        chip.className = "activity-task";
        chip.textContent = task;
        chips.appendChild(chip);
      });
      row.appendChild(chips);
    }

    row.addEventListener("click", () => openActivityDetail(run));
    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openActivityDetail(run);
    });
    return row;
  }

  async function openActivityDetail(run) {
    if (!run || !run.run_id) return;
    openActivityRun = run.run_id;
    els.detailRun.textContent = run.run_id;
    els.detailBody.textContent = "";
    const loading = document.createElement("div");
    loading.className = "detail-empty";
    loading.textContent = "正在读取……";
    els.detailBody.appendChild(loading);
    els.detailModal.classList.add("open");
    els.detailBackdrop.classList.add("open");

    try {
      // 详情只在打开时取一次；不参与 1.2 秒轮询。
      const data = await api(`/api/activities/${encodeURIComponent(run.run_id)}`);
      if (openActivityRun !== run.run_id) return;
      renderActivityDetail(data.activity || run);
      markActivityRead(run);
    } catch (error) {
      if (openActivityRun !== run.run_id) return;
      els.detailBody.textContent = "";
      const message = document.createElement("div");
      message.className = "detail-empty";
      message.textContent = `读取失败：${error.message}`;
      els.detailBody.appendChild(message);
    }
  }

  function renderActivityDetail(run) {
    const box = els.detailBody;
    box.textContent = "";
    els.detailRun.textContent = `${run.run_id} · ${activityTime(run.started_at)}`;
    const detail = run.detail || {};

    const meta = document.createElement("dl");
    meta.className = "detail-meta";
    [
      ["类型", activityKind(run.kind)],
      ["结果", activityOutcome(run).label],
      ["alarm", detail.alarm_id || "—"],
      ["scheduled_for", run.scheduled_for || "—"],
      ["开始", run.started_at || "—"],
      ["结束", run.finished_at || "—"],
      ["job", run.job_id || "—"],
      ["tasks", (detail.task_ids || []).join(", ") || "—"],
    ].forEach(([key, value]) => {
      const wrap = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = key;
      const desc = document.createElement("dd");
      desc.textContent = String(value);
      wrap.append(term, desc);
      meta.appendChild(wrap);
    });
    box.appendChild(meta);

    if (detail.error) {
      const section = document.createElement("section");
      section.className = "detail-section";
      const heading = document.createElement("h3");
      heading.textContent = "唤醒错误（原样）";
      const pre = document.createElement("pre");
      pre.textContent = String(detail.error);
      section.append(heading, pre);
      box.appendChild(section);
    }

    const calls = Array.isArray(detail.calls) ? detail.calls : [];
    const section = document.createElement("section");
    section.className = "detail-section";
    const heading = document.createElement("h3");
    heading.textContent = `LLM 调用输出（${calls.length} 次，原样）`;
    section.appendChild(heading);
    if (!calls.length) {
      const empty = document.createElement("div");
      empty.className = "detail-empty";
      empty.textContent = "这次唤醒没有模型输出。";
      section.appendChild(empty);
    } else {
      calls.forEach((call) => section.appendChild(renderActivityCall(call)));
    }
    box.appendChild(section);
  }

  function renderActivityCall(call) {
    const box = document.createElement("div");
    box.className = "call";

    const head = document.createElement("div");
    head.className = "call-head";
    const step = document.createElement("span");
    step.className = "call-step";
    step.textContent = `#${call.step || "?"}`;
    const label = document.createElement("span");
    label.className = "call-label";
    const decisions = call.decisions || [];
    if ((call.actions || []).length) {
      label.textContent = "动作步骤";
    } else if (decisions.length === 1 && decisions[0] === "inspect") {
      label.textContent = "读取事实";
    } else if (decisions.length) {
      label.textContent = "最终决定";
    } else {
      label.textContent = "未识别的输出";
    }
    head.append(step, label);
    const problem = call.decision_error || call.action_error;
    if (problem) {
      const error = document.createElement("span");
      error.className = "call-error";
      error.textContent = `parse: ${problem}`;
      head.appendChild(error);
    }

    const pre = document.createElement("pre");
    pre.textContent = String(call.raw_output || "");
    box.append(head, pre);

    if (call.receipt) {
      const receipt = document.createElement("div");
      receipt.className = "call receipt";
      const receiptHead = document.createElement("div");
      receiptHead.className = "call-head";
      const receiptLabel = document.createElement("span");
      receiptLabel.className = "call-meta";
      receiptLabel.textContent = "动作结果（原样）";
      receiptHead.appendChild(receiptLabel);
      const receiptPre = document.createElement("pre");
      receiptPre.textContent = String(call.receipt);
      receipt.append(receiptHead, receiptPre);
      box.appendChild(receipt);
    }
    return box;
  }

  function closeActivityDetail() {
    openActivityRun = "";
    els.detailModal.classList.remove("open");
    els.detailBackdrop.classList.remove("open");
  }

  async function markActivityRead(run) {
    const agentId = (state && state.front_agent) || "";
    if (!agentId) return;
    try {
      await api("/api/activities/seen", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId, run_id: run.run_id }),
      });
      const cutoff = String(run.started_at || "");
      (activityItems || []).forEach((item) => {
        if (!cutoff || String(item.started_at || "") <= cutoff) {
          item.unread = false;
        }
      });
      renderActivities();
      const unread = Number((state && state.activity_unread) || 0);
      if (unread <= 1) {
        els.activityBadge.textContent = "0";
        els.activityBadge.classList.add("zero");
      }
    } catch (_error) {
      // 已读写入失败不影响阅读本身。
    }
  }

  async function markAllActivitiesRead() {
    const agentId = (state && state.front_agent) || "";
    if (!agentId) return;
    try {
      await api("/api/activities/seen", {
        method: "POST",
        body: JSON.stringify({ agent_id: agentId }),
      });
      (activityItems || []).forEach((item) => { item.unread = false; });
      renderActivities();
      els.activityBadge.textContent = "0";
      els.activityBadge.classList.add("zero");
      lastActivityUnread = 0;
    } catch (error) {
      showToast(`标记已读失败：${error.message}`, true);
    }
  }

  /* ---------------- Agent 记忆 ---------------- */
  function memoryAgents() {
    const agents = ((state && state.agents) || []).filter(
      (agent) => !agent.archived);
    const list = agents.map((agent) => ({
      agent_id: agent.agent_id,
      name: agent.name || agent.agent_id,
      role: agent.agent_id === ((state && state.front_agent) || "")
        ? "Front" : "Agent",
    }));
    if (!list.length) {
      const frontId = (state && state.front_agent) || "agent_a";
      list.push({ agent_id: frontId, name: frontId, role: "Front" });
    }
    return list;
  }

  function memoryAgentName(agentId) {
    const agent = memoryAgents().find((item) => item.agent_id === agentId);
    return agent ? agent.name : agentId;
  }

  function populateMemoryAgents() {
    const list = memoryAgents();
    els.memoryAgent.textContent = "";
    list.forEach((agent) => {
      const option = document.createElement("option");
      option.value = agent.agent_id;
      option.textContent = `${agent.name}（${agent.agent_id}）`;
      els.memoryAgent.appendChild(option);
    });
    if (!list.some((agent) => agent.agent_id === memoryAgentId)) {
      memoryAgentId = (state && state.front_agent)
        || (list[0] && list[0].agent_id) || "";
    }
    els.memoryAgent.value = memoryAgentId;
  }

  function setMemoryOpen(open) {
    memoryOpen = Boolean(open);
    els.memoryModal.classList.toggle("open", memoryOpen);
    els.memoryBackdrop.classList.toggle("open", memoryOpen);
    els.memoryBtn.classList.toggle("active", memoryOpen);
    els.memoryBtn.setAttribute("aria-expanded", String(memoryOpen));
    if (memoryOpen) {
      populateMemoryAgents();
      loadMemory(memoryAgentId);
    }
  }

  async function loadMemory(agentId) {
    if (!agentId) return;
    const token = ++memoryRequestToken;
    memoryLoading = true;
    try {
      const data = await api(
        `/api/memory?agent_id=${encodeURIComponent(agentId)}`);
      if (token !== memoryRequestToken || agentId !== memoryAgentId) return;
      memoryData = data;
      renderMemory();
    } catch (error) {
      if (token !== memoryRequestToken) return;
      memoryData = null;
      els.memoryBody.textContent = "";
      const box = document.createElement("div");
      box.className = "memory-empty";
      const title = document.createElement("strong");
      title.textContent = "记忆读取失败";
      const detail = document.createElement("div");
      detail.textContent = error.message;
      box.append(title, detail);
      els.memoryBody.appendChild(box);
    } finally {
      if (token === memoryRequestToken) memoryLoading = false;
    }
  }

  function memoryNode(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function memoryTime(value) {
    const text = String(value || "");
    if (!text) return "—";
    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return text;
    const pad = (number) => String(number).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-`
      + `${pad(date.getDate())} ${pad(date.getHours())}:`
      + `${pad(date.getMinutes())}`;
  }

  function renderMemory() {
    const data = memoryData;
    const agentSelect = els.memoryAgent;
    const agentId = memoryAgentId || agentSelect.value;
    const agent = memoryAgents().find((item) => item.agent_id === agentId);
    els.memorySubtitle.textContent = agent
      ? `${agent.name}（${agent.agent_id}）· ${agent.role}`
      : agentId;
    if (!data) {
      els.memoryStats.textContent = "";
      els.memoryShortCount.textContent = "";
      els.memoryLongCount.textContent = "";
      return;
    }
    const short = data.short || { revision: 0, entries: [] };
    const long = data.long || [];
    const shortEntries = short.entries || [];
    els.memoryStats.textContent = "";
    [
      ["短期 revision", short.revision],
      ["短期条目", shortEntries.length],
      ["长期条目", long.length],
    ].forEach(([label, value]) => {
      const chip = memoryNode("span", "memory-stat");
      chip.appendChild(document.createTextNode(label));
      chip.appendChild(memoryNode("b", "", String(value)));
      els.memoryStats.appendChild(chip);
    });
    if (short.needs_compaction) {
      els.memoryStats.appendChild(memoryNode(
        "span", "memory-stat warn",
        "短期记忆超过一半，建议抽象成长期记忆"));
    }
    els.memoryShortCount.textContent = String(shortEntries.length);
    els.memoryLongCount.textContent = String(long.length);
    els.memoryClear.disabled = !shortEntries.length && !long.length;

    els.memoryBody.textContent = "";
    const items = memoryTab === "short" ? shortEntries : long;
    if (!items.length) {
      const box = memoryNode("div", "memory-empty");
      const title = memoryNode("strong", "", memoryTab === "short"
        ? "还没有短期记忆" : "还没有长期记忆");
      box.appendChild(title);
      box.appendChild(document.createTextNode(memoryTab === "short"
        ? "Agent 在对话 / 工作里写入后，会出现在这里。"
        : "短期记忆积累后，可以用 long_term_memory_consolidate 抽象成长期记忆。"));
      els.memoryBody.appendChild(box);
      return;
    }
    items.forEach((entry) => {
      els.memoryBody.appendChild(memoryTab === "short"
        ? renderShortMemory(entry) : renderLongMemory(entry));
    });
  }

  function memoryEntryActions(entry, kind) {
    const actions = memoryNode("div", "memory-entry-actions");
    const remove = memoryNode("button", "memory-mini danger", "删除");
    remove.type = "button";
    remove.addEventListener("click", () => deleteMemoryEntry(kind, entry));
    actions.appendChild(remove);
    return actions;
  }

  function renderShortMemory(entry) {
    const card = memoryNode("div", "memory-entry");
    const top = memoryNode("div", "memory-entry-top");
    top.appendChild(memoryNode(
      "span", `memory-badge imp${entry.importance}`,
      `importance ${entry.importance}`));
    top.appendChild(memoryNode("span", "memory-date",
      memoryTime(entry.created_at)));
    top.appendChild(memoryNode("span", "memory-id", entry.entry_id));
    top.appendChild(memoryEntryActions(entry, "short"));
    card.appendChild(top);
    card.appendChild(memoryNode("p", "memory-summary", entry.summary));
    if (entry.notable) {
      card.appendChild(memoryNode("p", "memory-notable", entry.notable));
    }
    if (entry.architecture) {
      card.appendChild(memoryNode("pre", "memory-arch", entry.architecture));
    }
    const meta = memoryNode("div", "memory-meta");
    meta.appendChild(memoryNode(
      "span", "memory-chip", `use_count ${entry.use_count}`));
    meta.appendChild(memoryNode(
      "span", "memory-chip mono", entry.task_id || "跨对话"));
    card.appendChild(meta);
    return card;
  }

  function renderLongMemory(entry) {
    const card = memoryNode("div", "memory-entry");
    const top = memoryNode("div", "memory-entry-top");
    top.appendChild(memoryNode(
      "span", "memory-badge weight",
      `weight ${Number(entry.weight || 0).toFixed(1)}`));
    top.appendChild(memoryNode("span", "memory-date",
      memoryTime(entry.created_at)));
    top.appendChild(memoryNode("span", "memory-id", entry.entry_id));
    const actions = memoryEntryActions(entry, "long");
    const ids = entry.short_term_entry_ids || [];
    let sub = null;
    if (ids.length) {
      const toggle = memoryNode(
        "button", "memory-mini", `关联短期 ${ids.length}`);
      toggle.type = "button";
      sub = memoryNode("div", "memory-sublist", ids.join(" · "));
      sub.hidden = true;
      toggle.addEventListener("click", () => { sub.hidden = !sub.hidden; });
      actions.insertBefore(toggle, actions.firstChild);
    }
    top.appendChild(actions);
    card.appendChild(top);
    card.appendChild(memoryNode("p", "memory-summary", entry.summary));
    if (sub) card.appendChild(sub);
    const meta = memoryNode("div", "memory-meta");
    meta.appendChild(memoryNode(
      "span", "memory-chip", `use_count ${entry.use_count}`));
    (entry.tasks || []).forEach((task) => {
      meta.appendChild(memoryNode("span", "memory-chip mono", task));
    });
    (entry.events || []).forEach((event) => {
      meta.appendChild(memoryNode("span", "memory-chip", event));
    });
    card.appendChild(meta);
    return card;
  }

  async function deleteMemoryEntry(kind, entry) {
    const ok = await confirmDialog({
      tone: "danger",
      title: kind === "short" ? "删除这条短期记忆？" : "删除这条长期记忆？",
      message: "删除后无法恢复。",
      detailsTitle: "将删除：",
      details: [String(entry.summary || entry.entry_id || "")],
      confirmText: "删除",
      cancelText: "取消",
    });
    if (!ok) return;
    try {
      const payload = {
        agent_id: memoryAgentId,
        kind,
        entry_id: entry.entry_id,
      };
      if (kind === "short") {
        payload.expected_revision = (memoryData.short || {}).revision || 0;
      }
      const data = await api("/api/memory/delete", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      memoryData = data;
      renderMemory();
      showToast("已删除记忆");
    } catch (error) {
      showToast(`删除失败：${error.message}`, true);
    }
  }

  async function clearMemory() {
    if (!memoryData) return;
    const shortCount = (memoryData.short || {}).entries?.length || 0;
    const longCount = (memoryData.long || []).length;
    const ok = await confirmDialog({
      tone: "danger",
      title: "清空这个 Agent 的记忆？",
      message: "短期记忆、归档和长期记忆都会被删除，无法恢复。",
      target: memoryAgentName(memoryAgentId),
      detailsTitle: "将删除：",
      details: [
        `短期记忆 ${shortCount} 条`,
        `长期记忆 ${longCount} 条`,
        "已归档的短期记忆",
      ],
      confirmText: "清空记忆",
      cancelText: "取消",
    });
    if (!ok) return;
    try {
      const data = await api("/api/memory/clear", {
        method: "POST",
        body: JSON.stringify({ agent_id: memoryAgentId }),
      });
      memoryData = data.memory;
      renderMemory();
      const deleted = data.deleted || {};
      const total = Number(deleted.short_term || 0)
        + Number(deleted.archived || 0)
        + Number(deleted.long_term || 0);
      showToast(`已清空 ${total} 条记忆记录`);
    } catch (error) {
      showToast(`清空失败：${error.message}`, true);
    }
  }

  function render() {
    if (!state) return;
    const settings = state.settings || {};
    const frontMode = settings.front_mode === "relay" ? "只传达" : "自主";
    const workAuto = settings.work_mode !== "workflow";
    const frontAgent = (state.agents || []).find(
      (agent) => agent.agent_id === state.front_agent);
    const frontLabel = (frontAgent && frontAgent.name) || state.front_agent || "—";
    els.frontBadge.textContent = `Front: ${frontLabel} · ${frontMode}`;
    els.frontBadge.classList.toggle("front-relay", frontMode === "只传达");
    els.workModeBadge.textContent = `Work: ${workAuto ? "自主" : "工作流"}`;
    els.workModeBadge.classList.toggle("work-auto", workAuto);
    els.workModeBadge.classList.toggle("work-flow", !workAuto);
    els.runningBadge.textContent = `Running: ${state.running_jobs || 0}`;
    const unread = Number(state.activity_unread || 0);
    els.activityBadge.textContent = String(unread);
    els.activityBadge.classList.toggle("zero", unread <= 0);
    if (els.activityBell) {
      els.activityBell.title = unread > 0
        ? `Agent 活动记录 · ${unread} 条未读`
        : "Agent 活动记录";
    }
    if (unread !== lastActivityUnread) {
      lastActivityUnread = unread;
      if (activityOpen) loadActivities();
    }
    if (memoryOpen) {
      const agents = memoryAgents();
      if (els.memoryAgent.options.length !== agents.length) {
        populateMemoryAgents();
      }
    }
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

  function newClientMessageId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return `msg_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  }

  function loadPendingSubmission() {
    try {
      const raw = localStorage.getItem(PENDING_SUBMIT_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (
        !parsed
        || typeof parsed.id !== "string"
        || typeof parsed.text !== "string"
        || Date.now() - Number(parsed.at || 0) > 24 * 60 * 60 * 1000
      ) {
        localStorage.removeItem(PENDING_SUBMIT_KEY);
        return;
      }
      pendingSubmission = parsed;
      if (!els.prompt.value.trim()) {
        els.prompt.value = parsed.text;
        showToast("已恢复上次未确认的提交，可直接重试");
      }
    } catch (_error) {
      localStorage.removeItem(PENDING_SUBMIT_KEY);
    }
  }

  function rememberPendingSubmission(text, workdir) {
    if (
      !pendingSubmission
      || pendingSubmission.text !== text
      || pendingSubmission.workdir !== workdir
    ) {
      pendingSubmission = {
        id: newClientMessageId(),
        text,
        workdir,
        at: Date.now(),
      };
      try {
        localStorage.setItem(
          PENDING_SUBMIT_KEY, JSON.stringify(pendingSubmission));
      } catch (_error) {
        // localStorage 不可用时，仍然保留内存里的幂等键。
      }
    }
    return pendingSubmission;
  }

  function clearPendingSubmission() {
    pendingSubmission = null;
    try { localStorage.removeItem(PENDING_SUBMIT_KEY); } catch (_error) {}
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
      const pending = rememberPendingSubmission(text, workdirPath);
      const submitted = await api("/api/submit", {
        method: "POST",
        body: JSON.stringify({
          text,
          workdir,
          client_message_id: pending.id,
        }),
      });
      pendingFront = {
        jobId: String(submitted.job_id || ""),
        startedAt,
      };
      clearPendingSubmission();
      els.prompt.value = "";
      forceMessagesScroll = true;
      await refresh();
      if (forceMessagesScroll) scrollMessagesToBottom();
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
  if (els.taskFilters) {
    els.taskFilters.addEventListener("click", (event) => {
      const button = event.target.closest("[data-task-filter]");
      if (!button) return;
      taskFilter = String(button.dataset.taskFilter || "all");
      els.taskFilters.querySelectorAll("[data-task-filter]").forEach((node) => {
        node.classList.toggle(
          "active", node.dataset.taskFilter === taskFilter);
      });
      lastTasksKey = "";
      renderTasks(state ? state.tasks : []);
    });
  }
  document.addEventListener("click", closeTaskMenus);
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
  els.confirmCancel.addEventListener("click", () => finishConfirm(false));
  els.confirmClose.addEventListener("click", () => finishConfirm(false));
  els.confirmOk.addEventListener("click", () => finishConfirm(true));
  els.confirmModal.addEventListener("click", (event) => {
    if (event.target === els.confirmModal) finishConfirm(false);
  });
  if (els.activityBell) {
    els.activityBell.addEventListener("click", () => {
      setActivityOpen(!activityOpen);
    });
    els.activityClose.addEventListener("click", () => setActivityOpen(false));
    els.activityBackdrop.addEventListener("click", () => setActivityOpen(false));
    els.activityMark.addEventListener("click", markAllActivitiesRead);
    els.detailBackdrop.addEventListener("click", closeActivityDetail);
    els.detailClose.addEventListener("click", closeActivityDetail);
    if (els.activityFilters) {
      els.activityFilters.addEventListener("click", (event) => {
        const button = event.target.closest("[data-filter]");
        if (!button) return;
        activityFilter = String(button.dataset.filter || "all");
        els.activityFilters.querySelectorAll("[data-filter]").forEach((node) => {
          node.classList.toggle("active", node === button);
        });
        renderActivities();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (els.detailModal.classList.contains("open")) {
        event.preventDefault();
        closeActivityDetail();
      } else if (activityOpen) {
        event.preventDefault();
        setActivityOpen(false);
      }
    });
  }
  if (els.memoryBtn) {
    els.memoryBtn.addEventListener("click", () => {
      setMemoryOpen(!memoryOpen);
    });
    els.memoryClose.addEventListener("click", () => setMemoryOpen(false));
    els.memoryBackdrop.addEventListener("click", () => setMemoryOpen(false));
    els.memoryClear.addEventListener("click", clearMemory);
    els.memoryAgent.addEventListener("change", () => {
      memoryAgentId = els.memoryAgent.value;
      memoryTab = "short";
      document.querySelectorAll("[data-memory-tab]").forEach((tab) => {
        tab.classList.toggle("active", tab.dataset.memoryTab === "short");
      });
      loadMemory(memoryAgentId);
    });
    document.querySelectorAll("[data-memory-tab]").forEach((tab) => {
      tab.addEventListener("click", () => {
        memoryTab = tab.dataset.memoryTab;
        document.querySelectorAll("[data-memory-tab]").forEach((other) => {
          other.classList.toggle("active", other === tab);
        });
        renderMemory();
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (els.memoryModal.classList.contains("open")) {
        event.preventDefault();
        setMemoryOpen(false);
      }
    });
  }

  document.addEventListener("keydown", (event) => {
    if (!confirmResolver) return;
    if (event.key === "Escape") {
      event.preventDefault();
      finishConfirm(false);
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = els.confirmDialog.querySelectorAll(
      "button:not([disabled]), [href], input:not([disabled]), select, textarea",
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
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
  loadPendingSubmission();
  refresh();
  setInterval(refresh, 1200);
})();
