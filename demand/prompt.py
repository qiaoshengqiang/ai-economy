"""
Demand Entry V1 的 Prompt 构造
================================

只陈述环境事实 + 可用动作，不替 Agent 决定团队/分工/产物。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from core.identity import format_agent_identity, format_agent_label
from core.write_limits import MAX_SINGLE_WRITE_CHARS

MAX_DIRECTORY_CHARS = 4_000
MAX_WORKSPACE_CHARS = 6_000
MAX_CONTEXT_CHARS = 5_000
MAX_INBOX_CHARS = 6_000


def render_directory(runtime: Any) -> str:
    lines: List[str] = []
    try:
        entries = sorted(runtime.directory.all(), key=lambda e: e.agent_id)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Agent Directory 读取失败：{type(exc).__name__}: {exc}）"
    for entry in entries:
        agent = None
        try:
            agent = runtime.economy.agent(entry.agent_id)
        except Exception:                                     # noqa: BLE001
            pass
        name = getattr(agent, "name", "") or entry.agent_id
        label = format_agent_label(name, entry.agent_id)
        caps = entry.capabilities.names if entry.capabilities else ()
        cap_text = ", ".join(sorted(caps)) if caps else "(none)"
        lines.append(
            f"- {label}: capabilities=[{cap_text}], "
            f"available={entry.available}")
    text = "\n".join(lines) if lines else "（目录为空）"
    return _clip(text, MAX_DIRECTORY_CHARS)


def _render_workspace(runtime: Any, workspace_id: str,
                      agent_id: str) -> str:
    gateway = runtime.workspace_gateway
    lines: List[str] = []
    root: Dict[str, Any] = {}

    def walk(path: str, depth: int) -> None:
        if len(lines) >= 120:
            return
        result = gateway.list(workspace_id, path, agent_id)
        if not result.success:
            return
        payload = result.result or {}
        if path == "/" and not root:
            root["controller"] = payload.get("effective_controller")
            root["lock"] = payload.get("effective_lock_holder")
        for entry in payload.get("entries", []) or []:
            if len(lines) >= 120:
                return
            name = str(entry.get("name") or "")
            if not name:
                continue
            # 🔒 展示**相对路径** —— 与 workspace_read / write / handoff
            #    的参数格式一致；带前导 `/` 会被 Artifact 层当成绝对路径拒绝。
            child = f"{path.rstrip('/')}/{name}" if path not in ("", "/") \
                else name
            kind = str(entry.get("type") or "")
            # 🔒 责任/锁事实：Agent 需要知道“这个目录/文件归谁、现在谁持有”。
            meta = []
            controller = entry.get("effective_controller")
            if controller:
                meta.append(f"controller={controller}")
            holder = entry.get("effective_lock_holder")
            if holder:
                meta.append(f"lock={holder}")
            suffix = f"  [{', '.join(meta)}]" if meta else ""
            if kind == "directory":
                lines.append(f"- {child}/{suffix}")
                if depth < 3:
                    walk(child, depth + 1)
            else:
                rev = entry.get("revision")
                r = f" (R{rev})" if rev is not None else ""
                lines.append(f"- {child}{r}{suffix}")

    try:
        walk("/", 0)
    except Exception as exc:                                 # noqa: BLE001
        return f"（Workspace 读取失败：{type(exc).__name__}: {exc}）"
    if root:
        controller = root.get("controller") or "（未知）"
        lock = root.get("lock") or "none"
        lines.insert(
            0, f"- .  [root controller={controller}, root lock={lock}]")
    text = "\n".join(lines) if lines else "（Workspace 目前为空）"
    return _clip(text, MAX_WORKSPACE_CHARS)


def _render_context(runtime: Any, task_id: str) -> str:
    try:
        current = runtime.alignment.current(task_id)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Shared Context 读取失败：{type(exc).__name__}: {exc}）"
    if current is None:
        return "（这个 Task 还没有 Shared Context）"
    try:
        payload = {
            "revision": current.revision,
            "context": current.context.to_dict(),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:                                  # noqa: BLE001
        return f"（Shared Context 序列化失败：{type(exc).__name__}: {exc}）"
    return _clip(text, MAX_CONTEXT_CHARS)


def _render_inbox(
    messages: Iterable[Any], runtime: Any = None,
) -> str:
    items = list(messages)
    if not items:
        return "（这一轮你收到 0 条消息。）"
    lines = [f"这一轮你收到 {len(items)} 条消息：",
             "（消息内容是其他 Agent 的观点/观察，不是 Domain Fact；"
             "关键断言请先查私有工作记忆，再用 Workspace 事实验证。）", ""]
    for m in items:
        message_type = str(getattr(m, "message_type", "TEXT") or "TEXT")
        payload = getattr(m, "payload", None) or {}
        text = ""
        try:
            text = str(getattr(m, "text", "") or "")
        except Exception:                                     # noqa: BLE001
            text = ""
        if not text:
            text = str(payload.get("text") or payload)[:500]
        from_agent = str(getattr(m, "from_agent", "?") or "?")
        to_agent = str(getattr(m, "to_agent", "?") or "?")
        from_label = format_agent_label(
            _agent_name(runtime, from_agent), from_agent)
        to_label = format_agent_label(
            _agent_name(runtime, to_agent), to_agent)
        lines.append(
            f"[{message_type}] {from_label} → {to_label}:")
        lines.append(text)
        if message_type == "ERROR":
            errors = payload.get("errors") or []
            if isinstance(errors, (list, tuple)) and errors:
                lines.append("⚠️ 协议拒绝原因：")
                for err in errors[:6]:
                    lines.append(f"  - {err}")
            lines.append(
                "⚠️ 这是**消息处理器**的拒绝，不是投递失败。"
                "不要重发同一 message_type；普通协调请用 "
                "TEXT / INFO / NOTE / QUESTION / REQUEST，"
                "或等 Task 状态允许再发生命周期消息。")
        lines.append("")
    return _clip("\n".join(lines).rstrip(), MAX_INBOX_CHARS)


def _render_lock_changes(items: List[Dict[str, Any]],
                         agent_id: str) -> str:
    """
    只展示**事实**：自这个 Agent 上次 turn 以来，锁从谁变到谁。

    🔒 不解释、不建议。A 这次看到 lock=agent_a，却误判成
       “锁一直在我手里”；把变更前后都写出来，避免历史推断。
    """
    if not items:
        return ""
    lines = ["========== 锁变更（自你上次 turn 以来；事实） =========="]
    for item in items[:10]:
        path = str(item.get("path") or "?").lstrip("/") or "/"
        old = str(item.get("from") or "none")
        new = str(item.get("to") or "none")
        lines.append(f"- {path}: lock {old} → {new}")
        if new == agent_id:
            lines.append(
                "  🔒 你现在持有这个资源的写锁；如果计划中的下一步是写它，"
                "可以直接 workspace_write / workspace_edit。")
        elif old == agent_id:
            lines.append("  你现在不再持有这个资源的写锁。")
    return "\n".join(lines)


def _render_stall_facts(
    *,
    read_only_streak: int,
    held_locks: List[str],
    pending_requests: List[Dict[str, Any]],
    stalled_agents: List[Dict[str, Any]],
) -> str:
    """
    只展示**动作历史事实**：谁连续只读、谁持锁、谁有未回应请求。

    🔒 不是墙钟压力，也不是指令；Agent 自己决定下一步。
    """
    lines: List[str] = []
    if read_only_streak >= 3:
        lines.append("========== 你的动作事实（不是指令） ==========")
        lines.append(
            f"- 你最近连续 {read_only_streak} 轮只有读取/搜索/查询，"
            "没有写入、编辑、handoff 或消息。")
        if held_locks:
            lines.append(
                "- 你当前持有写锁：" + ", ".join(
                    str(x).lstrip("/") or "/" for x in held_locks[:5]))
        if pending_requests:
            lines.append(
                f"- 有 {len(pending_requests)} 条未回应的 REQUEST/QUESTION。")
        lines.append(
            "- 如果被锁、缺信息或补丁不可用，handoff / 明确回复"
            "通常比继续重复读取更有效；如果这是有意的，请继续。")
    if stalled_agents:
        lines.append("========== 其他 Agent 的动作事实（不是指令） ==========")
        for item in stalled_agents[:4]:
            who = str(item.get("agent_id") or "?")
            streak = int(item.get("read_only_streak") or 0)
            locks = item.get("held_locks") or []
            pending = int(item.get("pending_requests") or 0)
            parts = [f"- {who}: 连续 {streak} 轮只有读取/搜索"]
            if locks:
                parts.append("持有写锁 " + ", ".join(
                    str(x).lstrip("/") or "/" for x in locks[:4]))
            if pending:
                parts.append(f"未回应请求 {pending} 条")
            lines.append("；".join(parts) + "。")
        lines.append(
            "- 这些只是事实；是否 handoff、改派或继续等待，由你决定。")
    return "\n".join(lines)


def _render_pending_requests(items: Optional[List[Dict[str, Any]]]) -> str:
    """把**尚未回应**的 REQUEST/QUESTION 放到 prompt 顶部。"""
    if not items:
        return ""
    lines = ["========== 未回应的请求（优先处理） =========="]
    for item in items[-3:]:
        who = str(item.get("from_agent") or "?")
        kind = str(item.get("message_type") or "REQUEST")
        turn = item.get("turn")
        text = str(item.get("text") or "").strip()
        lines.append(f"- turn {turn} · {who} [{kind}]")
        lines.append(text)
        lines.append("")
    lines.append(
        "这一轮优先：对上述请求给出**动作**（handoff / 拆件 / 写入）"
        "或明确回复；不要继续无关的重复读取。"
        "请求内容是对方的**观点**，先用你的私有工作记忆和 "
        "Workspace 事实验证，再决定是否照做。")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（截断：完整 {len(text)} 字符；这里只给前 {limit}）"


def _safe(render: Any, fallback: str) -> str:
    try:
        return str(render())
    except Exception as exc:                                  # noqa: BLE001
        return f"{fallback}：{type(exc).__name__}: {exc}"


def _render_drafts(items: Optional[List[Dict[str, Any]]]) -> str:
    """私有草稿元数据。🔒 不注入全文；全文由 draft_read 分块读取。"""
    drafts = list(items or [])
    if not drafts:
        return ("========== 你的私有草稿（不是项目文件） ==========\n"
                "（当前没有草稿。需要先草稿化时用 draft_write。）")
    lines = ["========== 你的私有草稿（不是项目文件） =========="]
    for item in drafts[:10]:
        draft_id = str(item.get("draft_id") or "?")
        label = str(item.get("label") or "")
        chars = item.get("chars")
        sha = str(item.get("sha256") or "")[:16]
        lines.append(
            f"- {draft_id}  label={label or '-'}  chars={chars}  "
            f"sha256={sha}…")
        preview = str(item.get("preview") or "").replace("\n", " ")[:200]
        if preview:
            lines.append(f"  preview: {preview}")
    if len(drafts) > 10:
        lines.append(f"  …还有 {len(drafts) - 10} 份草稿，用 draft_list 查看。")
    lines.append(
        "promotion 用 workspace_promote_drafts；一次可以写多个文件，"
        "不需要每轮只写一个。草稿不是 artifact，只有 promotion 才落盘。")
    return "\n".join(lines)


def _render_plan_status(runtime: Any, workspace_id: str, agent_id: str,
                        plan: Optional[Dict[str, Any]],
                        checkpoints: Optional[Dict[str, Any]] = None) -> str:
    """
    把 Creator 的 workspace_plan 与 Workspace 实际状态做对照。

    🔒 这是**系统派生的客观检查**，不是公共工作记忆，也不是 Task 状态。
    🔒 `phases` 的阶段检查点是 **Agent 自己记录的结果**，不是系统验收；
       系统只负责显示依赖/文件存在性和记录。
    """
    if not isinstance(plan, dict) or not workspace_id:
        return ""
    directories = plan.get("directories") or {}
    files = plan.get("files") or {}
    if not directories and not files:
        return ""
    gateway = getattr(runtime, "workspace_gateway", None)
    if gateway is None:
        return ""
    placeholders = {
        str(item) for item in (plan.get("placeholders") or [])
    }
    lines = ["========== 计划完成度（Creator workspace_plan） =========="]
    missing = 0

    def _exists(path: str, is_dir: bool) -> bool:
        try:
            if is_dir:
                return bool(gateway.list(
                    workspace_id, path, agent_id).success)
            return bool(gateway.read(
                workspace_id, path, agent_id).success)
        except Exception:                                     # noqa: BLE001
            return False

    if directories:
        lines.append("directories:")
        for path, owner in directories.items():
            ok = _exists(str(path), True)
            missing += 0 if ok else 1
            lines.append(
                f"- {path}/  {'✅' if ok else '❌ missing'}  "
                f"(owner={owner})")
    if files:
        lines.append("files:")
        for path, owner in files.items():
            path = str(path)
            ok = _exists(path, False)
            if not ok:
                missing += 1
                state = "❌ missing"
            elif path in placeholders:
                state = "⬜ placeholder（已创建，尚未实现）"
            else:
                state = "✅"
            lines.append(f"- {path}  {state}  (owner={owner})")
    if missing:
        lines.append(
            "⚠️ 以上是 Creator 计划的客观缺口；缺失项需要 Agent 动作补齐，"
            "不会因为其它文件写成功而自动完成。")
    else:
        lines.append("✅ 计划中的目录与文件都已存在。")

    phases = plan.get("phases") or []
    if isinstance(phases, list) and phases:
        known = checkpoints if isinstance(checkpoints, dict) else {}
        passed = {
            str(name) for name, item in known.items()
            if isinstance(item, dict) and item.get("passed")
        }
        lines.append("")
        lines.append(
            "========== 计划阶段（Creator workspace_plan；"
            "检查点是 Agent 记录，不是系统验收） ==========")
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            name = str(phase.get("name") or "")
            deps = [str(x) for x in (phase.get("depends_on") or [])]
            phase_files = [str(x) for x in (phase.get("files") or [])]
            missing_files = [
                f for f in phase_files if not _exists(f, False)]
            checkpoint = known.get(name) if isinstance(known, dict) else None
            checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
            waiting = [d for d in deps if d not in passed]
            if checkpoint.get("passed"):
                state = (f"✅ checkpoint passed (by "
                         f"{checkpoint.get('by') or '?'}；self-report)")
            elif checkpoint and checkpoint.get("passed") is False:
                state = (f"❌ checkpoint failed (by "
                         f"{checkpoint.get('by') or '?'}；self-report)")
            elif waiting:
                state = "⏳ pending；waiting=" + ", ".join(waiting)
            elif missing_files:
                state = "❌ files missing: " + ", ".join(missing_files)
            else:
                state = "▶ files present；checkpoint not recorded"
            extras: List[str] = []
            if checkpoint.get("passed") and missing_files:
                extras.append("⚠️ checkpoint passed 但文件缺失："
                              + ", ".join(missing_files))
            phase_placeholders = [
                f for f in phase_files if f in placeholders]
            if phase_placeholders:
                extras.append("⬜ placeholder：" + ", ".join(phase_placeholders))
            lines.append(
                f"- {state}  {name}  depends_on={deps or []}  "
                f"files={phase_files or []}")
            target = str(phase.get("checkpoint") or "").strip()
            if target:
                lines.append(f"  checkpoint 目标：{target}")
            for extra in extras:
                lines.append(f"  {extra}")
    return "\n".join(lines)


def _render_budget(runtime: Any, agent_id: str, *,
                   cap: Optional[int],
                   feedback: Optional[Dict[str, Any]]) -> str:
    """
    渲染**认知预算事实**。

    🔒 这是观测，不是指令：Agent 自己决定下一轮要不要调整。
    🔒 必须同时给出 requested / actual / clipped —— 否则 Agent
       无法知道“我请求了 32k，实际只拿到 16k”。
    """
    lines: List[str] = []
    if cap is not None:
        lines.append(f"- 本次运行的硬上限（Human）：{int(cap)} tokens")
    else:
        lines.append("- 本次运行没有额外的 Human 硬上限（Provider 结构上限仍生效）")
    try:
        agent = runtime.economy.agent(agent_id)
        brain = getattr(agent, "brain", None)
    except Exception:                                         # noqa: BLE001
        brain = None
    prefs = getattr(brain, "cognitive_budget_preferences", None)
    if callable(prefs):
        try:
            state = prefs() or {}
            lines.append(
                f"- config 默认偏好：{state.get('config_default')} tokens")
            per_phase = state.get("per_phase") or {}
            if per_phase:
                phase_text = ", ".join(
                    f"{k}={v}" for k, v in sorted(per_phase.items()))
                lines.append(f"- 当前分相偏好：{phase_text}")
            else:
                lines.append("- 当前分相偏好：（没有覆盖，使用 config 默认）")
        except Exception as exc:                              # noqa: BLE001
            lines.append(f"- 预算偏好读取失败：{type(exc).__name__}: {exc}")
    if feedback:
        lines.append(
            f"- 上一轮：turn={feedback.get('turn')} "
            f"phase={feedback.get('phase')} "
            f"finish_reason={feedback.get('finish_reason')} "
            f"output_status={feedback.get('output_status')}")
        lines.append(
            f"  requested={feedback.get('requested')} "
            f"actual={feedback.get('actual')} "
            f"clipped={feedback.get('clipped') or 'none'} "
            f"truncated={bool(feedback.get('truncated'))} "
            f"truncated_segments={feedback.get('truncated_segments') or 0} "
            f"segments={feedback.get('segments')}")
        if feedback.get("truncated"):
            lines.append(
                "  ⚠️ 上一轮有段被截断：该段即使解析出合法 Action 也不会执行。")
        if feedback.get("repair_truncated"):
            lines.append(
                "  ⚠️ 上一轮有一次调用撞到输出上限，但格式修复成功；"
                "这次调用仍计入工作单元压力。")
        if feedback.get("clipped") in ("above_caller_limit", "above_max"):
            lines.append(
                "  ⚠️ 上一轮请求的预算被裁剪；actual 才是真正可用的上限。")
        elif feedback.get("clipped") == "below_min":
            lines.append(
                "  ⚠️ 上一轮请求的预算低于 Provider 下限；actual 是抬升后的值。")
        if feedback.get("pressure"):
            streak = feedback.get("pressure_streak") or 0
            total = feedback.get("pressure_total") or 0
            kinds = feedback.get("pressure_kinds") or "unknown"
            lines.append(
                f"  ⚠️ 工作单元压力：连续第 {streak} 次，"
                f"本 run 累计 {total} 次（{kinds}）。")
            lines.append(
                "     事实：提高预算只改变单次输出上限，"
                "不改变工作单元大小。")
            if streak >= 2 or total >= 2:
                lines.append(
                    "     🔴 同一任务已累计多次超限；继续整文件/大块追加"
                    "很可能重复失败。优先**停止大块写入**，重新评估工作拆分"
                    "（拆成小文件/模块，或把部分交给其他 participant）。")
            else:
                lines.append(
                    "     如果当前仍是一次大块写入/生成，"
                    "可重新评估工作拆分。")
            lines.append(
                "     可选方向：拆成更小的文件/模块/步骤；"
                "把独立部分交给其他 participant；"
                "若 clipped=above_*，硬上限已到，再提高请求不会生效。")
        elif (feedback.get("pressure_total") or 0) >= 2:
            lines.append(
                f"  - 本 run 已累计 {feedback.get('pressure_total')} 次"
                f"预算/输出超限（最近一次 turn "
                f"{feedback.get('pressure_last_turn')}）。这是历史事实；"
                "如果当前仍是一次大块写入/生成，可重新评估工作拆分。")
    else:
        lines.append("- 上一轮：这是你这个 Agent 的第一次 turn。")
    lines.append(
        "- 你可以在信封顶层用 `cognitive_budget` 调整下一轮偏好："
        "`32768`（当前相）或 "
        "`{\"work\":32768,\"decision\":8192}`（分相）。")
    return "\n".join(lines)


def _envelope_example() -> str:
    return (
        '{"decision":"act","reasoning":"一句话说明你为什么这样做",'
        '"action":{"type":"动作类型","parameters":{...}}}'
    )


def build_brief_prompt(*, run: Any, front_agent: str,
                       runtime: Any = None,
                       previous_error: str = "") -> str:
    """
    Stage 1 —— **Front / 解读**。

    🔒 这个阶段**不做架构、不选参与者、不创建 Task**。
       它只把 Human Demand 变成 Creator 可用的 brief。
    """
    retry = ""
    if previous_error:
        retry = (
            "\n上一轮 brief 输出不合法：" + str(previous_error) +
            "。请只输出一个合法 JSON 对象。\n")
    identity = format_agent_identity(
        _agent_name(runtime, front_agent), front_agent)
    return f"""{identity}

你是 AI Economy OS 的 Human → OS 入口。
这是正式运行，不是你直接执行任务的阶段。

========== Human Demand ==========
{run.demand}

========== 运行约束（环境事实） ==========
- 生产预算：{run.budget_aic} AIC（观测；不是 Task.price）
- 墙钟上限：{run.wall_limit_seconds / 60:.1f} 分钟
- 最多参与者：{run.max_agents}

========== 你现在的角色：Front / 解读 ==========
你只负责解读 Human Demand：
- 目标是什么；
- 交付物有哪些；
- 约束是什么；
- 验收期望是什么；
- 有哪些歧义 / 非目标。
{retry}
🔒 不要做架构设计、模块拆分、参与者选择、producer/acceptor 选择，
不要调用 `task_create`，不要输出 `action`。

========== 输出格式（只输出 JSON，不要 markdown 围栏） ==========
{{"brief": {{
  "goal": "一句话目标",
  "deliverables": ["..."],
  "constraints": ["..."],
  "acceptance_expectations": ["..."],
  "ambiguities": ["..."],
  "non_goals": ["..."]
}}}}
"""


def build_intake_prompt(*, run: Any, runtime: Any, front_agent: str,
                        budget_cap: Optional[int] = None,
                        budget_feedback: Optional[Dict[str, Any]] = None,
                        brief: Optional[Dict[str, Any]] = None,
                        previous_error: str = "") -> str:
    directory = render_directory(runtime)
    short_term = _safe(
        lambda: runtime.private_short_term_memory.render(front_agent),
        "（短期记忆读取失败）")
    long_term = _safe(
        lambda: runtime.private_long_term_memory.render_top(front_agent),
        "（长期记忆读取失败）")
    budget = _render_budget(
        runtime, front_agent, cap=budget_cap, feedback=budget_feedback)
    brief_text = (json.dumps(brief, ensure_ascii=False, indent=2)
                  if brief else "（没有 Front brief；请直接根据 Human Demand 规划）")
    error_text = ""
    if previous_error:
        error_text = (
            "\n========== 上一轮 task_create 失败（请修正后重试） ==========\n"
            + str(previous_error) + "\n")
    identity = format_agent_identity(
        _agent_name(runtime, front_agent), front_agent)
    return f"""{identity}

你是 AI Economy OS 里的一名 Agent。这是**正式运行**，不是演示。

========== Human Demand ==========
{run.demand}

========== Front brief（只来自 Human Demand 的解读，不是架构方案） ==========
{brief_text}
{error_text}
========== 运行约束（环境事实） ==========
- 生产预算：{run.budget_aic} AIC（观测；不是 Task.price）
- 墙钟上限：{run.wall_limit_seconds / 60:.1f} 分钟
- 最多参与者：{run.max_agents}
- 当前没有 Task

========== 系统目标与协作（环境事实） ==========
- AI Economy OS 的目标之一：在保证质量的前提下，降低 AIC / token
  消耗、缩短墙钟、提高有效产出。低成本、高效率、高质量都是目标。
- 合理的分工与协作通常有助于减少单个 Agent 的上下文负担、匹配不同
  capabilities、减少返工，从而改善成本和有效产出。
- 当前 Task 调度是**串行轮转**（不是并行执行）：增加参与者会带来
  协调成本；是否分工、用多少参与者，由你决定。
- V1 没有 Child Task；Task 内的模块拆分通过文件/目录 +
  `resource_create` / `resource_handoff` 完成。
- Agent Directory 里的 capabilities 是公开事实，可用于判断谁适合
  承担哪个模块。

========== Workspace 锁与责任（结构事实） ==========
- Task 创建后，Workspace root 的 Directory Lock 会交给 producer（V1）。
- 如果 producer 不是你，你不会持有 root lock，不能在根目录下创建
  index.html / README.md / MODULE_CONTRACT.md 等根级文件。
- 创建子目录 / 新文件需要父目录的 Directory Lock。
- 新文件默认继承父目录责任；只有当前持有人能 handoff。
- `workspace_plan.root_owner` 必须等于 producer。
- `workspace_plan.files` 里的**根级文件** owner 必须是 root_owner。
- 目录/文件 owner 必须是 participants 之一；子目录的父目录必须在
  plan 的 directories 里声明。

========== 认知预算（观测，不是指令） ==========
{budget}

========== 你的短期记忆（跨任务摘要，只有你能读） ==========
{short_term}

========== 你的长期记忆（高权重摘要，只有你能读） ==========
{long_term}

========== Agent Directory（公开事实，不是“你应该找谁”） ==========
{directory}

========== 你的选择 ==========
1. 接受这个 Demand，创建一个 **Root Task**；
2. 如果你判断无法完成，可以普通回复说明原因（不创建 Task）。

如果创建 Task，必须由你调用 `task_create`。参数：
- `description`: 任务目标（可以把 Human Demand 概括进去）
- `participants`: 参与者 agent_id 列表，**必须包含你自己**，至少 2 人
- `acceptance_criteria`: Definition of Done，至少 1 条
- `producer`（必填）：负责实际生产的 Agent；必须是 participants 之一。
  如果你自己生产，填你自己。
- `acceptance_assignee`（可选）：验收责任人；必须是 participants 之一，
  不能是 creator 自己，也不能和 producer 相同。
- `workspace_plan`（必填）：责任计划。形状：
  {{"root_owner":"<producer>",
    "directories":{{"src":"<owner>","tests":"<owner>"}},
    "files":{{"index.html":"<root_owner>","src/main.js":"<owner>"}}}}
  root_owner 必须是 producer；根级文件 owner 必须是 root_owner。

🔒 验收人不能是生产者；生产者不得自验。
🔒 参与者在创建时**直接指定**（V1）；没有邀请/接受流程。
🔒 V1 只允许一个 Root Task；这个 run 再调用 `task_create` 会被拒绝。

输出格式（只输出 JSON 信封，不要 markdown 围栏）：
{_envelope_example()}

示例（只是形状，不要照抄内容）：
{{"decision":"act","reasoning":"接受 Demand，创建根任务",
  "action":{{"type":"task_create","parameters":{{
    "description":"...\\n拆分计划：\\n- 模块 A：...\\n- 模块 B：...\\n- 集成：...\\n- 验收：...",
    "participants":["{front_agent}","agent_b"],
    "acceptance_criteria":["交付物存在且非空","核心功能可运行"],
    "producer":"{front_agent}",
    "acceptance_assignee":"agent_b",
    "workspace_plan":{{"root_owner":"{front_agent}",
      "directories":{{"src":"{front_agent}"}},
      "files":{{"index.html":"{front_agent}"}}}}
  }}}}}}

如果任务包含多个独立模块，可以在 `description` 里附上拆分计划，
并让 participants 覆盖这些模块（示例只是形状，不规定必须几个人）。
"""


def _agent_name(runtime: Any, agent_id: str) -> str:
    try:
        return str(getattr(runtime.economy.agent(agent_id), "name", "") or "")
    except Exception:                                         # noqa: BLE001
        return ""


def build_task_prompt(
    *,
    run: Any,
    task: Any,
    runtime: Any,
    agent_id: str,
    inbox: Iterable[Any],
    budget_cap: Optional[int] = None,
    budget_feedback: Optional[Dict[str, Any]] = None,
    pending_requests: Optional[List[Dict[str, Any]]] = None,
    lock_changes: Optional[List[Dict[str, Any]]] = None,
    read_only_streak: int = 0,
    held_locks: Optional[List[str]] = None,
    stalled_agents: Optional[List[Dict[str, Any]]] = None,
    drafts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    directory = render_directory(runtime)
    workspace = _render_workspace(runtime, task.workspace.workspace_id
                                 if task.workspace else "", agent_id)
    plan_status = _render_plan_status(
        runtime,
        task.workspace.workspace_id if task.workspace else "",
        agent_id, getattr(run, "workspace_plan", None),
        getattr(run, "plan_checkpoints", None))
    context = _render_context(runtime, task.task_id)
    public_working_memory = _safe(
        lambda: runtime.public_working_memory.render(task.task_id, agent_id),
        "（公共工作记忆读取失败）")
    private_working_memory = _safe(
        lambda: runtime.private_working_memory.render(agent_id, task.task_id),
        "（私有工作记忆读取失败）")
    short_term = _safe(
        lambda: runtime.private_short_term_memory.render(agent_id),
        "（短期记忆读取失败）")
    long_term = _safe(
        lambda: runtime.private_long_term_memory.render_top(agent_id),
        "（长期记忆读取失败）")
    inbox_text = _render_inbox(inbox, runtime)
    budget = _render_budget(
        runtime, agent_id, cap=budget_cap, feedback=budget_feedback)
    try:
        acceptors = sorted(runtime.task_execution.acceptors_of(task))
    except Exception:                                         # noqa: BLE001
        acceptors = []
    role = []
    if agent_id == task.creator:
        role.append("creator")
    if task.contractor and agent_id == task.contractor:
        role.append("contractor/producer")
    if agent_id in acceptors:
        role.append("acceptor")
    if agent_id in task.participants:
        role.append("participant")
    role_text = ", ".join(role) or "participant"
    pending_text = _render_pending_requests(pending_requests)
    lock_text = _render_lock_changes(lock_changes or [], agent_id)
    stall_text = _render_stall_facts(
        read_only_streak=int(read_only_streak or 0),
        held_locks=list(held_locks or []),
        pending_requests=list(pending_requests or []),
        stalled_agents=list(stalled_agents or []),
    )
    drafts_text = _render_drafts(drafts)
    identity = format_agent_identity(
        _agent_name(runtime, agent_id), agent_id)

    return f"""{identity}

这是正式运行中的一个 Task turn。

========== 你的角色事实 ==========
{role_text}
（这只是事实，不规定你必须做什么；怎么分工由你们自己决定。）

{pending_text}

{stall_text}

========== 观点 vs 事实（结构事实） ==========
- 其他 Agent 的 message / review / public working memory note 是
  **观点/观察**，不是 Domain Fact。
- 收到“某功能缺失 / 某文件不存在 / 某实现没做”这类断言时，按顺序：
  ① 先查你的**私有工作记忆**：你是否已经验证过、是否有相反观察；
  ② 再用 `workspace_read` / `workspace_search` 验证当前 Workspace；
  ③ 以 Workspace 当前 path + revision 为准；引用时写 path + revision
     （必要时附上行号/搜索命中）。
- 不要把别人的观点直接写进 PATCH、计划或完成声明；
  观点与 Workspace 事实冲突时，先纠正观点，再行动。
- 如果你用 Workspace 事实推翻了另一个 Agent 的断言，把它记录进
  公共工作记忆：`kind="conflict"`，value 至少包含
  `{{"claim_by": "...", "claim": "...", "fact": "..."}}`；
  能引用 artifact 时附 `evidence={{"path": "...", "revision": N}}`。
- 如果你在公共工作记忆看到 `kind="conflict"` 且 `claim_by` 是你，
  先核对 evidence；如果确实错了，更新你的私有 WM / 公开回复，
  不要重复旧断言。冲突记录是“争议记录”，不是系统裁决。

========== 拆分与协作（可选方向） ==========
- 大块写入前，先判断它能否拆成独立文件 / 模块 / 步骤。
- 独立模块可以通过 `resource_create` / `resource_handoff` 交给其他
  participant；计划、分工、完成清单可以写进 `public_working_memory`。
- 一个 producer + 一个巨大文件不是默认方案；串行墙钟通常随模块数增长。
- AI Economy OS 的目标之一是低 AIC、短墙钟、高质量；合理分工通常
  有助于减少单个 Agent 的上下文负担和返工。是否拆分由你决定。

========== 规划与落地顺序（结构事实） ==========
- `workspace_plan` 可包含 `phases`：
  `[{{"name": "...", "depends_on": [], "files": [...], "checkpoint": "..."}}]`。
  `phases` 是开发顺序/依赖，不是 Task 状态；`checkpoint` 只是自由文本目标。
- 先建目录 + 入口 skeleton + 模块最小骨架，再按 phases 填充；
  不要先写大量内容，最后才发现入口/import 不存在。
- 需要 `resource_handoff` 的文件/目录必须**已经存在**：先创建，再 handoff。
- `placeholders` 可标记“已创建但尚未实现”的文件；实现后从 placeholders 移除。
- 每个 phase 结束时，**你自己选择**检查方式（`run_tool` / workspace_read /
  其他），再用 `workspace_plan_checkpoint` 记录 passed/evidence/note。
  系统不硬编码“每阶段必须跑 DOM/import/smoke”；Web 任务的最终 SUBMIT
  仍有单独的 DOM/import 门禁。
- 生成草稿时可以使用当前允许的最高 `cognitive_budget`；
  promotion / 审查 / 决策用较低预算。不要把高预算无差别套到所有 turn。
- 先 `draft_write`/`draft_append` 生成，再决定路径；多个文件准备好后
  用**一次** `workspace_promote_drafts` 落盘，不要每轮只写一个文件。

========== Task Core（唯一生命周期权威） ==========
- task_id: {task.task_id}
- status: {task.status}
- creator: {task.creator}
- contractor/producer: {task.contractor or "（还没有）"}
- participants: {", ".join(task.participants)}
- acceptance assignees: {", ".join(acceptors) or "（还没有）"}
- workspace_id: {task.workspace.workspace_id if task.workspace else "（无）"}
- description: {task.description}

========== 认知预算（观测，不是指令） ==========
{budget}

========== Task Shared Context（共同认知） ==========
{context}

========== 公共工作记忆（Task 范围，参与者可见） ==========
{public_working_memory}

========== 私有工作记忆（只有你自己可见；验证他人断言的第一站） ==========
{private_working_memory}

{drafts_text}

========== 你的短期记忆（跨任务摘要） ==========
{short_term}

========== 你的长期记忆（高权重摘要） ==========
{long_term}

========== Workspace 事实（当前版本；下面都是相对路径） ==========
{workspace}

{lock_text}

{plan_status}

🔒 `workspace_read` / `workspace_search` / `workspace_write` /
`workspace_edit` / `resource_handoff` 的 `path` **不要带开头 `/`**；
`.` 表示 Workspace root。

========== Workspace 责任与验收通道（结构事实） ==========
- 创建子目录 / 新文件需要**父目录**的有效 Directory Lock。
- 如果 root controller / root lock 不是你，你无法直接在根下创建 tests/。
  正确路径：请持有者创建目录并 `resource_handoff` 给你；
  或把测试文件写进你的 private workspace，再用
  `run_tool(source="private")` 执行（只适用于不依赖 Task Workspace
  其他文件的测试）。
- 被锁拒绝后不要反复重试同一个 `resource_create`；先看上面的
  root controller / lock，或发消息请持有者 handoff。

========== 这一轮的信箱 ==========
{inbox_text}

========== Agent Directory（公开事实） ==========
{directory}

========== 你可以使用的动作 ==========
- `workspace_read`
  可给 `offset`（1-based 起始行）/ `limit`（最多行数）；显式范围最多 400 行。
- `workspace_search`
  `query` / `path`（可选）/ `glob`（可选）/ `max_results`（可选）/
  `case_sensitive` / `context_lines`；只读，返回 path/line/snippet。
- `workspace_write`
  write 必须给 `write_mode`: create / replace / append。
  create/replace 需要输出完整文件；能局部改就不要用 replace。
  🔒 单次 `content` 最多 {MAX_SINGLE_WRITE_CHARS} 字符；超限直接拒绝
  （`content_too_large`），**不会写入**。这只在确实超过单次输出预算时才发生；
  大文件可以一次写入，不必为了旧上限做无意义拆分。
- `workspace_edit`
  `path` / `old_string` / `new_string`；old_string 必须唯一命中；
  可选 `replace_all` / `expected_revision`。
  仍然走锁 / revision / audit；0 或多个命中会失败。
  🔒 单次 `new_string` 最多 {MAX_SINGLE_WRITE_CHARS} 字符；超限直接拒绝，
  只有确实超过单次输出预算时才需要拆成多次编辑。
- `message_send`
  `to` / `message_type` / `text` / `payload`
  **普通协调消息**用 `TEXT` / `INFO` / `NOTE` / `QUESTION` / `REQUEST`。
  **生命周期消息**只有四个：
  `START`（仅 contractor，Task 为 CREATED/ACCEPTED）、
  `SUBMIT`（仅 contractor，IN_PROGRESS）、
  `VERIFY`（仅 acceptor，SUBMITTED）、
  `COMPLETE`（VERIFIED）。
  **不要用 START 表示“开始一条消息 / 开始一轮沟通 / 开始验收”。**
  发送成功只代表**已投递**；生命周期是否被接受要看 Task 状态或 ERROR 回执。
- `assign_acceptance`
  把验收责任指派给另一个 participant（不能给自己）
- `shared_context_read`
- `run_tool`
  `tool`: python / pytest / node / playwright；`args` 是字符串列表；
  `source`（可选，task/private）与 `path`（可选）决定复制哪些文件到 run 目录；
  可选 `timeout_seconds`。
- `public_query`：读公共资料区（含 SYSTEM_MAP 和 tools/*.md）。
- `private_write/read/list/delete`：你自己的私有文件空间。
- `draft_write` / `draft_append` / `draft_read` / `draft_list` /
  `draft_delete`：**私有草稿区**。
  `draft_write(content, label?)` 返回 draft_id；`draft_append` 分块追加；
  `draft_read(draft_id, offset?, limit?)` 按字符范围读取。
  草稿不是项目文件，不参与验收；系统不会自动 promotion。
- `workspace_promote_drafts`
  `items`: `[{{"draft_id": "...", "path": "...",
  "write_mode": "create|replace|append"}}]`。
  **一次 Action 可以落盘多个文件**；按 items 顺序走 Workspace Gateway
  （锁 / revision / audit 照旧）。草稿内容不会重新经过模型输出。
  如果多个文件都准备好了，应该在**同一轮** promote，不要逐个文件拆轮。
- `resource_create` / `resource_handoff` / `resource_release`：
  建目录 / 交接责任 / 释放独立责任。
- `workspace_plan_update`（仅 creator）
  `plan`: 完整的 `{{"root_owner": ..., "directories": ..., "files": ...,
  "phases": [...], "placeholders": [...]}}`。
  交付范围 / ownership / 开发顺序变化时使用；更新后“计划完成度”以新 plan 为准，
  不会继续显示旧 plan 里已经移除的文件。
- `workspace_plan_checkpoint`
  `phase` / `passed`（布尔）/ `evidence`（可选对象）/ `note`（可选）。
  记录某个 phase 的检查结果；检查方式由你选择（run_tool / read / 其他）。
  它是 Agent 的阶段检查记录，不是 Task 验收。
- `public_working_memory_read`
- `public_working_memory_update`
  `expected_revision` / `items` / `remove_keys` / `content`
  `items`: `key -> {{kind, value, parent_key?, evidence?}}`；
  kind 只支持 plan / assignment / checklist / progress / note /
  conflict。conflict 的 value 至少包含 claim_by / claim / fact；
  `parent_key` 表达层级（大架构 → 局部细节）；
  `evidence`: `{{"path":"真实 artifact 路径","revision":N}}`，系统会校验路径/revision；
  没有 evidence 的条目会显示为 self-report，不是已验证事实。
  `content` 是“单条 note”的简写。
  🔒 不要在写文件的同一个信封里声称完成：先执行 workspace_write/edit，
  拿到 success + revision，再在后续更新中用 evidence 记录完成。
- `private_working_memory_read`
- `private_working_memory_update`
  `expected_revision` / `nodes` / `remove_nodes`
  `nodes`: `node_id -> {{kind, title, content?, parent_id?}}`；
  kind 只支持 goal / branch / task / finding / issue / note；
  `parent_id` 表达你自己的抽象分解树。
  工作记忆是**你自己的记录**；系统不自动写、不自动总结。
- `short_term_memory_read/update/reinforce`：跨任务短期记忆。
- `long_term_memory_search/read/mark_used/consolidate`：长期记忆 + 权重。
  `workspace_search` 只读，**不会**把结果写进任何记忆。

========== 工具与执行边界（结构事实） ==========
- `run_tool` 在**一次性 run 副本**里执行。对副本的写（下载、生成、删除）
  **不会回写 Task Workspace**。要交付文件，必须用 `workspace_write` /
  `workspace_edit` 写入 Workspace。
- Playwright 需要**先写测试文件**：在 Workspace 写 `tests/foo.spec.js`，
  再 `run_tool(tool="playwright", args=["tests/foo.spec.js"])`；
  它会先跑 smoke gate。不要用 `run_tool(python/node)` 去导入 Playwright
  （Python 环境没有这个包）。
- 工具说明在公共资料区：
  `public_query(name="tools/python.md")` ·
  `public_query(name="tools/pytest.md")` ·
  `public_query(name="tools/node.md")` ·
  `public_query(name="tools/playwright.md")`。
- 验收人如果**无法复跑验证**，应发 `VERIFY(passed=false)` 并写明缺口/证据；
  不要用重复 `workspace_read` / `run_tool` 代替验收结论。

========== 生命周期规则（协议硬约束） ==========
- START 不是“开始发言/开始验收/发起请求”，它只把 Task 从
  CREATED/ACCEPTED 推进到 IN_PROGRESS。
  普通协调一律用 TEXT / INFO / NOTE / QUESTION / REQUEST。
- 如果你不是 contractor/producer：**不要发 START / SUBMIT**。
  发 START / SUBMIT 会被状态机拒绝。
- 收到 `ERROR` 回执 = 上一条消息被**协议拒绝**（不是没收到）。
  不要重发同一 message_type；改用普通类型，或等 Task 状态允许。
- 如果你是 contractor/producer 且 Task 还是 CREATED：先发 START。
  例：{{"to":"{task.creator}","message_type":"START",
       "payload":{{"task_id":"{task.task_id}"}}}}
- 如果你是 contractor/producer 且 Task 是 IN_PROGRESS：
  完成工作后发 SUBMIT。载荷必须包含：
  {{"to":"<任意 participant>","message_type":"SUBMIT","payload":{{
    "task_id":"{task.task_id}",
    "result":{{"result_ref":"<逻辑名，不能是路径/URL>",
              "result_type":"code",
              "summary":"<一句话>"}},
    "completion_proposal":{{
      "criteria_status":"MET",
      "evidence":[{{"result_ref":"<同一个逻辑名>",
                   "result_type":"code",
                   "summary":"<证据说明>"}}],
      "outstanding_gaps":[]
    }}
  }}}}
- 如果你不是 creator/acceptor：**不要发 VERIFY / COMPLETE**。
- 如果你是 acceptor：只在 Task 为 SUBMITTED 后发 VERIFY。
  载荷：{{"to":"<producer>","message_type":"VERIFY",
          "payload":{{"task_id":"{task.task_id}","passed":true,
                     "feedback":"<独立复跑证据>"}}}}
- VERIFY 通过后，再由 creator 或 acceptor 发 COMPLETE：
  {{"to":"<producer>","message_type":"COMPLETE",
    "payload":{{"task_id":"{task.task_id}","summary":"..."}}}}
- 如果还没有验收人，由**非 producer** 用 `assign_acceptance` 指派。
- DoD 在创建时已写入；不要凭空改 Task 状态。

========== 输出格式 ==========
只输出 JSON 信封，不要 markdown 围栏：
{_envelope_example()}
可选：在信封顶层放 `thinking`（按相设置思考强度）或
`cognitive_budget`（当前相整数 / 分相对象），只影响**下一步**。
`thinking` 推荐形状（`enabled` 必须是布尔值；`effort` 可选）：
  {{"thinking": {{"enabled": true, "effort": "high"}}}}
  或分相：
  {{"thinking": {{"decision": {{"enabled": false, "effort": "low"}},
                "work": {{"enabled": true, "effort": "high"}}}}}}
`effort` 取值：low / medium / high / max / minimal / xhigh / ultra / none。
不写就保持当前策略。
如果没有动作，可以普通回复（不产生 Action）。
"""
