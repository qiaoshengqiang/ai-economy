"""
FrontAgentRuntime —— Human 意图入口
====================================

    Human 输入
        ↓
    FrontAgentRuntime.decide()
        ├─ direct_reply
        ├─ handoff  → FrontBrief
        ├─ rework   → 原 Task 的 Creator
        ├─ follow_up→ 进行中 Task 的 Creator
        ├─ cancel   → 停止 / 取消 Task
        ├─ artifacts→ 导出 Task 产物到宿主机
        └─ clarify

🔒 Front Agent 永不创建 Task。
   它可以对话、使用自己的私有空间 / 草稿，并输出 Decision / Brief；
   Task 由承接的 Agent 创建。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from front.contracts import (
    FrontDecision, FrontIntent, FrontResult, FrontTurn,
)
from front.parser import (
    parse_front_actions, parse_front_decision, parse_front_decisions,
)


#: Front 调用的 system prompt —— 只描述入口职责。
#:
#: 🔴 不写 Agent 身份 / 经济 / Constitution：那些属于接单与议价，
#:    不属于「把 Human 的话路由到哪里」。
FRONT_SYSTEM_PROMPT = (
    "你是 Agent 系统中面向人类的入口。"
    "你可以聊天、使用自己的私有 workspace 和草稿，并把工作交给 Work Agent。"
    "请使用人类使用的语言回复。"
)


class FrontUnavailableError(RuntimeError):
    """Front 本次没有产出可用决定；这是可重试的失败，不是 clarify。"""


class FrontAgentRuntime:
    """一个可测试的 Front Agent（LLM 调用通过 `think` 注入）。"""

    def __init__(
        self,
        agent_id: str,
        think: Callable[[str], str],
        *,
        inspect_fn: Optional[Callable[[str], Any]] = None,
        roles: Iterable[Dict[str, Any]] = (),
        execute_actions: Optional[
            Callable[[List[Dict[str, Any]]], str]] = None,
        action_types: Iterable[str] = (),
        max_steps: int = 12,
        think_retries: int = 1,
        front_mode: str = "autonomous",
    ) -> None:
        if not callable(think):
            raise ValueError("FrontAgentRuntime 需要可调用的 think(prompt)")
        self.agent_id = str(agent_id)
        self._think = think
        #: 只读 inspect 能力：task_id → 事实（dict/str）。None = 不可用。
        self._inspect_fn = inspect_fn
        #: Front 可见的 Worker 角色目录（id/name/description）。
        self.roles = [
            dict(item) for item in (roles or ())
            if isinstance(item, dict) and item.get("id")
        ]
        #: Front 自己的动作执行器（私有空间 / 草稿）。
        self._execute_actions = execute_actions
        self.action_types = tuple(sorted({
            str(item) for item in action_types if str(item).strip()}))
        self.max_steps = max(1, int(max_steps))
        self.think_retries = max(0, int(think_retries))
        mode = str(front_mode or "autonomous").strip().lower()
        if mode not in ("autonomous", "relay"):
            mode = "autonomous"
        self.front_mode = mode
        #: 最近一次 decide 的动作历史 / 原始输出（观测用）。
        self.last_action_history: List[Dict[str, Any]] = []
        self.last_raw_output: str = ""

    # ------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------
    def decide(self, human_text: str, **kwargs: Any) -> FrontDecision:
        """兼容旧调用：返回本回合的第一个 decision。"""
        return self.decide_turn(human_text, **kwargs).primary

    def decide_turn(
        self,
        human_text: str,
        *,
        active_tasks: Iterable[Dict[str, Any]] = (),
        completed_tasks: Iterable[Dict[str, Any]] = (),
        conversation: Iterable[Dict[str, Any]] = (),
        agents: Iterable[Dict[str, Any]] = (),
        context: str = "",
        max_inspect: int = 2,
    ) -> FrontTurn:
        """
        识别 Human 意图。

        `active_tasks` / `completed_tasks` 是 **只读引用**：
        `{"task_id", "summary", "status"}`。Front 不修改它们。

        `conversation` 是最近 Human / Front 消息的只读快照。

        `agents` 是系统里的 Agent 事实：`{agent_id, status, role, can_work}`。
        Front 可以据此决定是否指定 `preferred_agent`；系统不规定它怎么做。

        `inspect` 是只读能力，不是最终决定；最多 `max_inspect` 次。
        """
        active = [
            _task_ref(item) for item in active_tasks
            if _task_ref(item)
        ]
        completed = [
            _task_ref(item) for item in completed_tasks
            if _task_ref(item)
        ]
        active_ids = {item["task_id"] for item in active}
        completed_ids = {item["task_id"] for item in completed}
        history = _conversation_refs(conversation)
        agent_facts = _agent_refs(agents)

        inspection: List[Dict[str, Any]] = []
        action_history: List[Dict[str, Any]] = []
        last_error = ""
        parse_failures = 0
        inspect_count = 0
        self.last_action_history = []
        self.last_raw_output = ""
        for _step in range(self.max_steps):
            prompt = self._prompt(
                human_text, active, completed, conversation=history,
                agents=agent_facts, inspection=inspection,
                action_history=action_history,
                context=context, previous_error=last_error)
            raw = self._think_with_retry(prompt)
            self.last_raw_output = str(raw or "")

            decisions, error = parse_front_decisions(
                raw,
                human_text=human_text,
                active_task_ids=active_ids,
                completed_task_ids=completed_ids,
            )

            # 动作先执行：同一份输出里既写文件又给出最终 intent 也成立。
            actions, action_error = parse_front_actions(raw)
            if actions:
                if self._execute_actions is None:
                    raise FrontUnavailableError(
                        "当前 runtime 不支持 Front 动作")
                receipt = self._execute_actions(actions)
                action_history.append({
                    "step": len(action_history) + 1,
                    "actions": actions,
                    "raw_output": str(raw or ""),
                    "receipt": str(receipt or ""),
                })
                self.last_action_history = list(action_history)
                parse_failures = 0
                last_error = ""
                if not decisions:
                    continue

            if decisions:
                if any(item.intent == FrontIntent.INSPECT
                       for item in decisions):
                    if len(decisions) != 1:
                        raise FrontUnavailableError(
                            "inspect 不能和其他最终动作放在同一回合")
                    decision = decisions[0]
                    if self._inspect_fn is None:
                        raise FrontUnavailableError(
                            "inspect_unavailable：Front 需要读取任务事实，"
                            "但当前没有 inspect 能力")
                    if inspect_count >= max_inspect:
                        raise FrontUnavailableError(
                            f"inspect_limit：本次最多 inspect {max_inspect} 次")
                    try:
                        facts = self._inspect_fn(decision.task_id)
                    except Exception as exc:                  # noqa: BLE001
                        facts = (
                            f"inspect_error: {type(exc).__name__}: {exc}")
                    inspection.append({
                        "task_id": decision.task_id,
                        "facts": facts,
                    })
                    inspect_count += 1
                    continue

                for item in decisions:
                    item.raw_output = str(raw or "")
                self.last_action_history = list(action_history)
                return FrontTurn(
                    decisions=decisions,
                    raw_output=str(raw or ""),
                    action_history=list(action_history),
                )

            last_error = action_error or error or "front_decision_not_found"
            parse_failures += 1
            if parse_failures >= 2:
                raise FrontUnavailableError(
                    f"Front 输出无法解析：{last_error}")

        raise FrontUnavailableError(
            f"Front 未在限定步数内给出最终决定："
            f"{self.max_steps} 步")

    def _think_with_retry(self, prompt: str) -> str:
        """LLM 调用失败时重试；最终失败交给上层标记可重试中断。"""
        attempts = self.think_retries + 1
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                return self._think(prompt)
            except Exception as exc:                          # noqa: BLE001
                last = exc
        raise FrontUnavailableError(
            f"Front LLM call failed after {attempts} attempt(s): "
            f"{type(last).__name__}: {last}")

    # ------------------------------------------------------------
    # 结果格式化
    # ------------------------------------------------------------
    @staticmethod
    def format_result(result: FrontResult) -> str:
        """Work Result → Human 可读文本。"""
        return result.to_human_text()

    @staticmethod
    def result_from_work(
        work_result: Any,
        *,
        artifacts: Optional[List[str]] = None,
        next_actions: Optional[List[str]] = None,
    ) -> FrontResult:
        """
        把 WorkResult（或同形状对象）转成 FrontResult。

        🔒 只读转换；不改变 Task 状态。
        """
        if hasattr(work_result, "to_dict"):
            data = work_result.to_dict()
        elif isinstance(work_result, dict):
            data = dict(work_result)
        else:
            data = {
                "task_id": getattr(work_result, "task_id", ""),
                "status": getattr(work_result, "status", ""),
                "summary": getattr(work_result, "summary", ""),
                "iteration": getattr(work_result, "iteration", 1),
                "error": getattr(work_result, "error", ""),
            }
        return FrontResult(
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or ""),
            summary=str(data.get("summary") or ""),
            artifacts=list(artifacts or data.get("artifacts") or []),
            next_actions=list(next_actions or data.get("next_actions") or []),
            iteration=int(data.get("iteration") or 1),
            error=str(data.get("error") or ""),
        )

    # ------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------
    def _prompt(
        self,
        human_text: str,
        active: List[Dict[str, str]],
        completed: List[Dict[str, str]],
        *,
        conversation: List[Dict[str, str]] = (),
        agents: List[Dict[str, Any]] = (),
        inspection: List[Dict[str, Any]] = (),
        action_history: List[Dict[str, Any]] = (),
        context: str = "",
        previous_error: str = "",
    ) -> str:
        error_text = ""
        if previous_error:
            error_text = (
                "\n上一次输出无法解析：" + previous_error +
                "。继续下一步；最终仍要输出一个合法的 JSON。\n")
        actions = ", ".join(self.action_types) or "（无）"
        mode_section = ""
        if self.front_mode == "relay":
            mode_section = """
========== Front 模式：只传达 ==========
当前是“只传达”模式，不做需求扩展。
- task_text 只保留用户的核心任务要求；保留用户明确提出的任务细节。
- 删除“我想……”“再给 agent_b 一个任务”等对话性、路由性和解释性文字。
- 不要添加用户没有提出的功能、技术栈、文件、验收标准、约束、实现建议或视觉细节。
- handoff 的 brief 只填 goal 和 task_text；
  deliverables / constraints / acceptance_expectations / files 留空。
- role / work_profile / preferred_agent 只作为路由元数据，不要写进 task_text。
"""
        return f"""你是 {self.agent_id}，面向人类的入口。

请自然对话，使用人类使用的语言。你可以使用自己的私有 workspace 和草稿，
也可以把工作交给 Work Agent。Task 由 Work Agent 创建；你没有创建 Task 的动作。

========== 最近对话（只读） ==========
{_render_conversation(conversation)}

========== 当前人类输入 ==========
{human_text}

========== Agent 事实（只读） ==========
{_render_agents(agents)}

========== Worker 角色（只读） ==========
{_render_roles(self.roles)}

========== 进行中的 Task（只读） ==========
{_render_tasks(active)}

========== 已完成的 Task（只读） ==========
{_render_tasks(completed)}

========== 检查结果（只读） ==========
{_render_inspection(inspection)}

========== Front 动作（可选，多步） ==========
可用动作：{actions}

你可以在最终 intent 之前返回只包含动作的步骤：
{{"actions":[{{"type":"private_write","parameters":{{"path":"notes.md","content":"..."}}}}]}}
{{"actions":[{{"type":"artifact_read","parameters":{{"task_id":"task_x","path":"index.html","offset":1,"limit":10}}}}]}}

动作会真实执行；结果会在下一次 prompt 返回。然后继续。
`artifact_read` 是一个**动作**，不是 intent：把它放进 `actions`，
不要放进 `intent`。
参数：
- artifact_read {{task_id, path, offset?, limit?}} —— 读取 Task
  Workspace 中的文件（只读；返回内容 + total_lines/revision）
- private_write {{path, content}}；private_read {{path}}；private_list {{}}
- private_delete {{path}}
- draft_write {{content, label}}；draft_append {{draft_id, content}}
- web_search {{query, count?, need_content?, ori_question?}} —— 查询外部实时信息
- web_fetch {{url}} —— 读取一个公开网页的 Markdown 内容
- draft_read {{draft_id}}；draft_list {{}}；draft_delete {{draft_id}}

能力事实：
- artifact_read 直接读取 Task Workspace 内容；用于
  “给我看代码 / Task 文件的前 N 行”。
- artifact_read 受 Task 范围限制；不能读取任意宿主机路径。
- `artifacts` intent 只把 Task 文件导出到宿主机并返回路径，**不返回**
  文件内容。
- 如果人类只问“文件在哪 / 目录在哪”，先用 inspect 获取 `workspace_path`，
  再用 direct_reply 回答；不要用 artifacts。
- web_search 是只读外部事实：只在当前事实不足、需要最新外部信息时使用；
  搜索结果是证据，不是结论；引用 URL，不要把整页内容抄进回复。
- 不要用 web_search 查询本地 Task / Workspace 已经能回答的问题。
- web_fetch 只用于公开网页；不要 fetch 内网、localhost 或私有地址。

handoff 可以携带你在 `brief.files` 里准备好的文件：
[{{"path":"index.html","content":"..."}}]
[{{"path":"index.html","from_private":"snake.html"}}]
[{{"path":"index.html","draft_id":"draft_..."}}]

========== 动作结果 / 状态 ==========
{_render_actions(action_history)}

========== 其他上下文 ==========
{context or "（无）"}
{error_text}
========== 交付模式（work_profile） ==========
- direct：单文件 / 单次操作，不需要运行程序或浏览器验证；
- standard：一个可运行或可检查的交付物，需要一次工程验证；
- creative_product：可交互产品、多文件、多模块、玩法/视觉/交互，需要真实运行验证。
无法判断时选 standard，不要默认 direct。
Role 只是弱信号，不要仅凭 role 判断。
{mode_section}
========== 意图 ==========
1. direct_reply —— 直接回答。
   {{"intent":"direct_reply","reply":"...","reason":"..."}}

2. handoff —— 新的工作请求。
   task_text 是 Work Agent 看到的内容；human_text 保留原文。
   preferred_agent 是路由元数据；空 / 省略 = 系统选择一个空闲 worker。
   brief 可以携带 goal / deliverables / constraints / acceptance_expectations
   和 files。
   {{"intent":"handoff",
     "task_text":"...",
     "role":"worker 角色 id 之一",
     "work_profile":"direct|standard|creative_product",
     "preferred_agent":"optional agent_id",
     "brief":{{"goal":"...","deliverables":["..."],
              "constraints":["..."],"acceptance_expectations":["..."],
              "files":[{{"path":"index.html","content":"..."}}]}},
     "reply":"...",
     "reply_style":"...","reason":"..."}}

3. rework —— 修改一个已完成的 Task。
   rework_scope：fix / enhance / redesign。
   {{"intent":"rework","task_id":"...","rework_scope":"enhance",
     "reply":"...","reason":"..."}}

4. follow_up —— 继续一个进行中 / 已中断的 Task。
   {{"intent":"follow_up","task_id":"...","reply":"...","reason":"..."}}

5. cancel —— 停止一个进行中的 Task。
   {{"intent":"cancel","task_id":"...","reply":"...","reason":"..."}}

6. artifacts —— 把 Task 文件导出到宿主机；返回路径，不返回内容。
   需要展示文件内容时使用 artifact_read。
   {{"intent":"artifacts","task_id":"...","reply":"...","reason":"..."}}

7. clarify —— 当选择很重要时询问人类。
   {{"intent":"clarify","question":"...","reason":"..."}}

8. inspect —— 只读 Task 事实；每次输入最多 2 次。
   返回 Task 的 workspace_path（Task Workspace 的宿主机路径）和文件列表；
   只读，不导出。
   {{"intent":"inspect","task_id":"...","reason":"..."}}

9. set_mode —— 修改 Front / Work 的全局模式，不创建 Task。
   front_mode：autonomous | relay（可选）
   work_mode：workflow | autonomous（可选）
   {{"intent":"set_mode","front_mode":"relay","work_mode":"autonomous",
     "reply":"...","reason":"..."}}
   未提供的字段保持不变。修改后对新任务生效。

一个回合内多个动作：
{{"decisions":[
  {{"intent":"cancel","task_id":"task_a","reply":"task_a 已停"}},
  {{"intent":"cancel","task_id":"task_b","reply":"task_b 已停"}}
]}}
控制动作（direct_reply / clarify / cancel / artifacts / set_mode）按你给出的顺序执行。
每个回合最多一个执行动作（handoff / rework / follow_up），它在控制动作之后执行。

路由事实：
- preferred_agent 为 IDLE + can_work → 分配给它。
- preferred_agent 为 WORKING → 拒绝；不会自动替换。
- 未知 / FRONT / 不能工作 → 拒绝。
- 没有 preferred_agent → 系统选择一个空闲 worker。

状态为 INTERRUPTED 的 Task 当前没有运行；follow_up 可以恢复它。
只输出 JSON，不要 markdown 围栏。
"""


def _render_actions(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "（无）"
    lines: List[str] = []
    for item in items:
        actions = item.get("actions") or []
        names = ", ".join(
            str(action.get("type") or "?")
            for action in actions if isinstance(action, dict))
        lines.append(f"步骤 {item.get('step', '?')}：{names}")
        receipt = str(item.get("receipt") or "").strip()
        if receipt:
            if len(receipt) > 12000:
                receipt = receipt[:12000] + "\n…（动作结果已截断）"
            lines.append(receipt)
    return "\n".join(lines)

def _conversation_refs(
    items: Iterable[Dict[str, Any]],
    *,
    limit: int = 12,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        text = str(item.get("text") or "").strip()
        if role not in ("human", "front") or not text:
            continue
        out.append({
            "role": "人类" if role == "human" else "Front Agent",
            "text": text[:1200],
        })
    return out[-max(0, int(limit)):]


def _render_conversation(items: List[Dict[str, str]]) -> str:
    if not items:
        return "（没有更早的对话）"
    return "\n".join(
        f"{item['role']}: {item['text']}" for item in items)


def _agent_refs(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id") or "").strip()
        if not agent_id:
            continue
        out.append({
            "agent_id": agent_id,
            "status": str(item.get("status") or ""),
            "role": str(item.get("role") or ""),
            "can_work": bool(item.get("can_work", True)),
        })
    return out


def _render_roles(roles: List[Dict[str, Any]]) -> str:
    if not roles:
        return "（无；使用 generalist）"
    return "\n".join(
        f"- {item['id']}: {item.get('name') or item['id']} — "
        f"{item.get('description') or ''}"
        for item in roles)


def _render_agents(agents: List[Dict[str, Any]]) -> str:
    if not agents:
        return "（无）"
    return "\n".join(
        f"- {item['agent_id']}：status={item['status'] or '?'} "
        f"role={item['role'] or 'WORKER'} can_work={item['can_work']}"
        for item in agents
    )


def _render_inspection(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "（无）"
    lines: List[str] = []
    for item in items:
        task_id = str(item.get("task_id") or "")
        facts = item.get("facts")
        if not isinstance(facts, (dict, list)):
            facts = str(facts or "")
        try:
            text = json.dumps(facts, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(facts)
        if len(text) > 6000:
            text = text[:6000] + "\n…（检查结果已截断）"
        lines.append(f"[{task_id}]\n{text}")
    return "\n\n".join(lines)


def _task_ref(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not isinstance(item, dict):
        return None
    task_id = str(item.get("task_id") or "").strip()
    if not task_id:
        return None
    return {
        "task_id": task_id,
        "status": str(item.get("status") or ""),
        "summary": str(item.get("summary") or "")[:500],
    }


def _render_tasks(items: List[Dict[str, str]]) -> str:
    if not items:
        return "（无）"
    lines = []
    for item in items:
        summary = f" — {item['summary']}" if item.get("summary") else ""
        status = f" [{item['status']}]" if item.get("status") else ""
        lines.append(f"- {item['task_id']}{status}{summary}")
    return "\n".join(lines)
