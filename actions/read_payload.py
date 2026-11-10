"""
把**读取类动作的结果**渲染成可以当场回喂给模型的一段文本
================================================================

【🔴 它补的是哪一个缺口（用户 2026-10-10 决定，形状 2「读完立刻动手」）】

    实测（运行 1010-1009）用真实渲染函数跑出来的证据：

        workspace_read → `output.content` = 2521 字符（整章正文），
                         DB 里完整存下来了
        Agent 下一轮**实际看到**的：
            action=workspace_read target=manuscript/chapter_001.md
            revision=1 chars=2521
        🔴 正文在里面吗 = False

    ⇒ **Agent 请求读文件，永远收不到文件内容。**
       「接口成功掩盖语义空转」—— 本项目自己的 bug 类②。

    `actions/workspace_read.py` 的 docstring 当时写着「读到的内容在
    下一轮 Cognition 才被 Agent 看到」—— **那句话是错的**：
    `cognition/context.py::_experience_detail` 只渲染一个白名单
    （`revision` / `chars` / `write_mode` / …），**`content` 不在里面**。

【本模块做什么】

    把「这一轮发起的读取」的真实返回渲染成一段文本，
    由 `run_cognition` 作为**同一次认知里的下一条 user 消息**回喂给模型。

    🔒 **不是 tool calling** —— 只是普通消息。
       `actions/__init__.py` 明列「❌ DSH Tool Bridge / Tool Registry」，
       走原生 `tools` 会撞那条边界；普通消息不会。

【🔴 三件不许做的事】

① **不许加解读**。只呈现环境真实返回的字段。
   §七：经历/观察只能渲染成**材料**，不能渲染成指令
   （不写「你应该…」「请据此…」）。

② **不许无上限**。读取可以拿到任意大的正文 ——
   没有上限的话，一次读取就能把上下文撑爆。
   超了就**如实说截断了**（含完整长度），不静默少给。

③ **不许猜一个动作的结果**。只渲染 `success=True` 并且**真的有载荷**
   的结果。失败的动作没有「读到的内容」，编一个空串会让人以为读到了。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from actions.intent import ActionIntent, ActionResult
from actions.private_workspace import (
    ACTION_PRIVATE_DELETE, ACTION_PRIVATE_LIST, ACTION_PRIVATE_READ,
    ACTION_PRIVATE_WRITE,
)
from actions.public_query import ACTION_PUBLIC_QUERY
from actions.resource_control import (
    ACTION_RESOURCE_CREATE, ACTION_RESOURCE_HANDOFF, ACTION_RESOURCE_RELEASE,
)
from actions.assignment import ACTION_ASSIGN_ACCEPTANCE
from actions.message_send import ACTION_MESSAGE_SEND
from actions.shared_context_read import ACTION_SHARED_CONTEXT_READ
from actions.workspace_read import ACTION_WORKSPACE_READ
from actions.workspace_write import ACTION_WORKSPACE_WRITE
from actions.workspace_edit import ACTION_WORKSPACE_EDIT
from actions.workspace_search import ACTION_WORKSPACE_SEARCH
from actions.run_tool import ACTION_RUN_TOOL
from actions.tool_help import ACTION_TOOL_HELP
from actions.public_working_memory import (
    ACTION_PUBLIC_WORKING_MEMORY_READ,
    ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
)
from actions.private_working_memory import (
    ACTION_PRIVATE_WORKING_MEMORY_READ,
    ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
)
from actions.private_short_term_memory import (
    ACTION_SHORT_TERM_MEMORY_READ,
    ACTION_SHORT_TERM_MEMORY_REINFORCE,
    ACTION_SHORT_TERM_MEMORY_UPDATE,
)
from actions.private_long_term_memory import (
    ACTION_LONG_TERM_MEMORY_CONSOLIDATE,
    ACTION_LONG_TERM_MEMORY_MARK_USED,
    ACTION_LONG_TERM_MEMORY_READ,
    ACTION_LONG_TERM_MEMORY_SEARCH,
)
from actions.workspace_draft import (
    ACTION_DRAFT_APPEND, ACTION_DRAFT_DELETE, ACTION_DRAFT_LIST,
    ACTION_DRAFT_READ, ACTION_DRAFT_WRITE,
    ACTION_WORKSPACE_PROMOTE_DRAFTS,
)

#: 结果里带**新的世界信息**、值得当场回喂给模型的动作。
#:
#: 判据是**语义**，不是「有没有 content 字段」：
#:     这些动作回答的是「环境现在是什么样」—— Agent 拿不到就等于没读。
OBSERVABLE_ACTIONS = frozenset({
    ACTION_WORKSPACE_READ,
    ACTION_WORKSPACE_SEARCH,
    ACTION_PRIVATE_READ,
    ACTION_PRIVATE_LIST,
    ACTION_PUBLIC_QUERY,
    ACTION_SHARED_CONTEXT_READ,
    ACTION_RUN_TOOL,
    ACTION_TOOL_HELP,
    ACTION_PUBLIC_WORKING_MEMORY_READ,
    ACTION_PRIVATE_WORKING_MEMORY_READ,
    ACTION_SHORT_TERM_MEMORY_READ,
    ACTION_LONG_TERM_MEMORY_SEARCH,
    ACTION_LONG_TERM_MEMORY_READ,
    ACTION_DRAFT_READ,
    ACTION_DRAFT_LIST,
})

#: 明确**不是**观察类 —— 它们的结果只是「我刚才做了什么」的确认。
#:
#: 🔒 显式列出来不是为了「用得上」，而是为了让
#:    「**新动作必须做一次选择**」成为硬要求：
#:    守卫 `tests/test_read_payload_feedback.py` 会检查每个注册的
#:    动作**要么**在 `OBSERVABLE_ACTIONS`、**要么**在这里。
#:    否则新动作会**静默**落进「不回喂」那一类 —— 那正是这个 bug 的成因。
CONFIRMATION_ACTIONS = frozenset({
    ACTION_WORKSPACE_WRITE,
    ACTION_WORKSPACE_EDIT,
    ACTION_MESSAGE_SEND,
    ACTION_ASSIGN_ACCEPTANCE,
    ACTION_RESOURCE_CREATE,
    ACTION_RESOURCE_HANDOFF,
    ACTION_RESOURCE_RELEASE,
    ACTION_PRIVATE_WRITE,
    ACTION_PRIVATE_DELETE,
    ACTION_PUBLIC_WORKING_MEMORY_UPDATE,
    ACTION_PRIVATE_WORKING_MEMORY_UPDATE,
    ACTION_SHORT_TERM_MEMORY_UPDATE,
    ACTION_SHORT_TERM_MEMORY_REINFORCE,
    ACTION_LONG_TERM_MEMORY_MARK_USED,
    ACTION_LONG_TERM_MEMORY_CONSOLIDATE,
    ACTION_DRAFT_WRITE,
    ACTION_DRAFT_APPEND,
    ACTION_DRAFT_DELETE,
    ACTION_WORKSPACE_PROMOTE_DRAFTS,
})

#: 只由 Demand / Work 组合根注册、但同样是真实动作的集合。
#:
#: `tests/action_registry_helpers.py` 的“注册动作”来源是生产实验；
#: 草稿动作只在 Demand 注册，`tool_help` 只在 Work 注册，
#: 所以守卫需要显式知道它们的存在。
DEMAND_ONLY_ACTIONS = frozenset({
    ACTION_DRAFT_WRITE,
    ACTION_DRAFT_APPEND,
    ACTION_DRAFT_READ,
    ACTION_DRAFT_LIST,
    ACTION_DRAFT_DELETE,
    ACTION_WORKSPACE_PROMOTE_DRAFTS,
    ACTION_TOOL_HELP,
})

#: 单条读取结果最多回喂多少字符。
#: 🔒 2521 字符的整章正文在实测里是正常量级；8000 留了余量，
#:    同时保证「一次读取」不会自己把上下文撑爆。
PER_PAYLOAD_MAX_CHARS = 8000

#: 这一轮所有读取结果加起来最多多少字符。
TOTAL_MAX_CHARS = 12000

#: 一次 follow-up 最多附几张图（视觉 token 有成本，不能无限塞）。
MAX_FEEDBACK_IMAGES = 2

#: 把 `(run_id, relative_path)` 解析成 data URL；返回 None = 这张图不可用。
VisualResolver = Callable[[str, str], Optional[str]]

#: 带 `content` 字段的动作 —— 直接就是那段文本。
_CONTENT_ACTIONS = frozenset({
    ACTION_WORKSPACE_READ, ACTION_PRIVATE_READ, ACTION_PUBLIC_QUERY,
    ACTION_DRAFT_READ, ACTION_TOOL_HELP,
})

#: 结构化载荷 —— 渲染成可读 JSON（保真，不做摘要）。
_STRUCTURED_ACTIONS = frozenset({
    ACTION_SHARED_CONTEXT_READ, ACTION_DRAFT_LIST,
})


def is_observable(action_type: Any) -> bool:
    return str(action_type or "") in OBSERVABLE_ACTIONS


def _clip(text: str, limit: int) -> Tuple[str, bool]:
    """返回 `(文本, 是否被截断)`。截断时**必须**能看出来（调用方会写明）。"""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _tool_output_text(output: Dict[str, Any]) -> str:
    """把 run_tool 的结构化结果渲染成 Agent 可读材料。"""
    lines = [
        f"tool={output.get('tool')}  "
        f"exit_code={output.get('exit_code')}  "
        f"ok={output.get('ok')}  "
        f"timed_out={output.get('timed_out')}  "
        f"duration_ms={output.get('duration_ms')}",
    ]
    if output.get("message"):
        lines.append(f"message: {output['message']}")
    stdout = str(output.get("stdout") or "")
    stderr = str(output.get("stderr") or "")
    if stdout:
        lines.extend(["", "--- stdout ---", stdout])
    if stderr:
        lines.extend(["", "--- stderr ---", stderr])
    if not stdout and not stderr:
        lines.append("（没有 stdout / stderr）")

    images = output.get("images") or []
    if isinstance(images, list) and images:
        lines.append("")
        lines.append(f"images: {len(images)} 张")
        for image in images:
            if not isinstance(image, dict):
                continue
            lines.append(
                f"  - {image.get('path')} "
                f"({image.get('mime') or 'image'}, {image.get('bytes')} bytes)")
    return "\n".join(lines)


def _payload_of(action_type: str, output: Any) -> Optional[str]:
    """
    从 `ActionResult.output` 里取出**可读的那部分**。

    🔒 只认已知形状；认不出来返回 None（＝「没有可回喂的载荷」），
       **不**把整个 output 一股脑 dump 出去 —— 那会把写动作的
       回显也带上（那是 Agent 自己刚给的内容，回喂没有意义）。
    """
    if not isinstance(output, dict):
        return None

    if action_type == ACTION_RUN_TOOL:
        return _tool_output_text(output)

    if action_type in (ACTION_PUBLIC_WORKING_MEMORY_READ,
                       ACTION_PRIVATE_WORKING_MEMORY_READ,
                       ACTION_SHORT_TERM_MEMORY_READ,
                       ACTION_LONG_TERM_MEMORY_SEARCH,
                       ACTION_LONG_TERM_MEMORY_READ):
        return json.dumps(output, ensure_ascii=False, indent=2)

    if action_type == ACTION_WORKSPACE_READ:
        content = output.get("content")
        if not isinstance(content, str) or not content:
            return None
        start = output.get("start_line")
        end = output.get("end_line")
        total = output.get("total_lines")
        if (isinstance(start, int) and isinstance(end, int)
                and isinstance(total, int)
                and (start > 1 or end < total)):
            return f"[lines {start}-{end} of {total}]\n{content}"
        return content

    if action_type == ACTION_WORKSPACE_SEARCH:
        matches = output.get("matches") or []
        lines = [
            f"query={output.get('query')!r}  "
            f"total={output.get('total_matches')}  "
            f"files={output.get('files_scanned')}  "
            f"truncated={output.get('truncated')}",
        ]
        for m in matches:
            if not isinstance(m, dict):
                continue
            lines.append(
                f"{m.get('path')}:{m.get('line')}: {m.get('snippet')}")
            for c in (m.get("context") or []):
                if isinstance(c, dict):
                    lines.append(f"  {c.get('line')}: {c.get('text')}")
        return "\n".join(lines)

    if action_type in _CONTENT_ACTIONS:
        content = output.get("content")
        return content if isinstance(content, str) and content else None

    if action_type == ACTION_DRAFT_LIST:
        drafts = output.get("drafts") or []
        if not drafts:
            return "（当前没有草稿）"
        lines = []
        for item in drafts:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('draft_id')}  label={item.get('label') or '-'} "
                f"chars={item.get('chars')}  sha256={item.get('sha256')}")
        return "\n".join(lines)

    if action_type == ACTION_PRIVATE_LIST:
        paths = output.get("paths")
        if not paths:
            return "（这个目录下没有文件）"
        return "\n".join(f"- {p}" for p in paths)

    if action_type in _STRUCTURED_ACTIONS:
        parts: List[str] = []
        context = output.get("context")
        if isinstance(context, dict):
            parts.append("共同认知（当前那一版）：")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        domain = output.get("domain_state")
        if isinstance(domain, dict):
            parts.append("")
            parts.append(
                "这个 Task 在 Task Core 里的客观状态"
                "（读的这一刻现场取的**引用**，不是共同认知的一份副本）：")
            parts.append(json.dumps(domain, ensure_ascii=False, indent=2))
        recent = output.get("recent_changes")
        if recent:
            parts.append("")
            parts.append("最近的变更：")
            parts.append(json.dumps(recent, ensure_ascii=False, indent=2))
        return "\n".join(parts) if parts else None

    # 认不出来的观察类动作：至少把 output 的**键**如实说出来，
    # 而不是编一个内容。这比静默什么都不给诚实。
    return None


def _target_of(intent: ActionIntent) -> str:
    """这一条读取读的是**什么** —— 只从真实字段取，不推断。"""
    params = dict(getattr(intent, "parameters", None) or {})
    for key in ("path", "name", "tool"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _workspace_read_signature(
        intent: ActionIntent, result: Optional[ActionResult]) -> Optional[tuple]:
    """
    `workspace_read` 的**版本 + 范围**指纹。

    🔒 同一 path + revision + start/end 的重复读取，不应该把同一段
       文件内容再次塞回上下文。revision 变了就重新回喂。
    """
    if str(getattr(intent, "action_type", "") or "") != ACTION_WORKSPACE_READ:
        return None
    if result is None or not getattr(result, "success", False):
        return None
    output = getattr(result, "output", None)
    if not isinstance(output, dict):
        return None
    params = dict(getattr(intent, "parameters", None) or {})
    path = params.get("path") or output.get("path")
    revision = output.get("revision")
    if not path or revision is None:
        return None
    return (str(path), revision,
            output.get("start_line"), output.get("end_line"))


def _picked(pairs: Iterable[Tuple[ActionIntent, Optional[ActionResult]]]
            ) -> List[Tuple[str, str, str]]:
    """
    `[(action_type, target, 载荷文本)]` —— **一次**遍历定下顺序。

    🔒 顺序 = Agent 声明的顺序。不要在这里排序或去重：
       「它先读 A 再读 B」是真实发生的事实。
    """
    out: List[Tuple[str, str, str]] = []
    for intent, result in pairs:
        if result is None or not getattr(result, "success", False):
            continue
        action_type = str(getattr(intent, "action_type", "") or "")
        if not is_observable(action_type):
            continue
        text = _payload_of(action_type, getattr(result, "output", None))
        if text:
            out.append((action_type, _target_of(intent), text))
    return out


def observable_results(
    pairs: Iterable[Tuple[ActionIntent, Optional[ActionResult]]],
) -> List[Tuple[str, str]]:
    """
    从 `[(intent, result)]` 里挑出**真的有载荷**的观察类结果。

    返回 `[(action_type, 载荷文本)]` —— 顺序 = Agent 声明的顺序。

    🔒 这里不做长度裁剪 —— 裁剪在 `render_read_results` 里做，
       因为**总上限**要看全部载荷。
    """
    return [(a, t) for a, _target, t in _picked(pairs)]


def _feedback_images(
    pairs: List[Tuple[ActionIntent, Optional[ActionResult]]],
    resolver: VisualResolver,
) -> List[Dict[str, Any]]:
    """
    把 `run_tool` 返回里的图片元数据解析成 **user 消息的 image parts**。

    🔒 `actions/` 是叶子层：这里**不读文件**，只调用注入的 resolver。
    🔒 最多 `MAX_FEEDBACK_IMAGES` 张；解析失败就跳过，不编造。
    """
    parts: List[Dict[str, Any]] = []
    for intent, result in pairs:
        if len(parts) >= MAX_FEEDBACK_IMAGES:
            break
        if str(getattr(intent, "action_type", "")) != ACTION_RUN_TOOL:
            continue
        output = getattr(result, "output", None)
        if not isinstance(output, dict):
            continue
        run_id = str(output.get("run_id") or "")
        images = output.get("images") or []
        if not isinstance(images, list):
            continue
        for image in images:
            if len(parts) >= MAX_FEEDBACK_IMAGES:
                break
            if not isinstance(image, dict):
                continue
            path = str(image.get("path") or "")
            if not path:
                continue
            try:
                data_url = resolver(run_id, path)
            except Exception:                                 # noqa: BLE001
                # 图片解析失败只影响附件，不影响文本回喂。
                data_url = None
            if not data_url:
                continue
            parts.append({
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            })
    return parts


def render_read_results(
    pairs: Iterable[Tuple[ActionIntent, Optional[ActionResult]]],
    *,
    per_payload_max: int = PER_PAYLOAD_MAX_CHARS,
    total_max: int = TOTAL_MAX_CHARS,
    image_resolver: Optional[VisualResolver] = None,
    seen_reads: Optional[set] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """
    渲染成一段可以当**下一条 user 消息**发的文本（或 content parts）。

    没有可回喂的载荷时返回 `""` —— 调用方据此决定**不做** follow-up
    （那正是「最多一次」这条硬上限的判据）。

    有图片时返回 `[{"type":"text",...}, {"type":"image_url",...}]`；
    没有图片时仍返回字符串（向后兼容既有调用方）。

    `seen_reads`（可选，跨 turn 共享的 set）：
        记录已经回喂过的 `workspace_read` 指纹
        `(path, revision, start_line, end_line)`。
        相同指纹再次出现时只报告“内容未重复发送”，不再塞一遍全文。
        不传时行为完全不变。

    🔒 措辞纪律：只陈述「这是你刚才请求的环境真实返回」，
       **不写**「你应该据此…」「请继续…」。
    """
    materialized = list(pairs)
    fresh: List[Tuple[ActionIntent, Optional[ActionResult]]] = []
    duplicate_notes: List[str] = []
    for pair in materialized:
        signature = _workspace_read_signature(pair[0], pair[1])
        if signature is not None and seen_reads is not None:
            if signature in seen_reads:
                duplicate_notes.append(
                    f"[{pair[0].action_type}] {_target_of(pair[0])} "
                    f"（相同 revision / 范围；内容未重复发送）")
                continue
            seen_reads.add(signature)
        fresh.append(pair)

    picked = _picked(fresh)
    image_parts = (_feedback_images(fresh, image_resolver)
                   if image_resolver is not None else [])
    if not picked and not image_parts and not duplicate_notes:
        return ""

    lines: List[str] = [
        "========== 环境对你刚才那次读取的真实返回 ==========",
        "",
    ]
    used = 0
    dropped = 0
    shown = 0

    for action_type, target, text in picked:
        full_len = len(text)
        body, cut = _clip(text, int(per_payload_max))

        head = f"[{shown + 1}] {action_type}"
        if target:
            head += f"  {target}"
        block = f"{head}\n{'-' * 60}\n{body}"
        if cut:
            # 🔒 截断必须**看得出来**，并说清完整长度 ——
            #    静默少给等于让模型以为「这就是全部」。
            block += (f"\n（⚠️ 这一条被**截断**了：完整内容 {full_len} 字符，"
                      f"这里只给出前 {len(body)} 字符。）")

        # 预算检查放在拼好之后 —— 已经给了至少一条时才会丢。
        if used + len(block) > int(total_max) and shown > 0:
            dropped += 1
            continue
        used += len(block)
        shown += 1
        lines.append(block)
        lines.append("")

    if dropped:
        lines.append(
            f"（⚠️ 还有 {dropped} 条读取结果因为**总长度上限**没有列出来 —— "
            f"它们真实发生过，这里如实说明，不假装只有上面这些。）")
        lines.append("")

    if duplicate_notes:
        lines.append(
            "========== 重复读取（相同 revision / 范围） ==========")
        lines.extend(duplicate_notes)
        lines.append("")

    text = "\n".join(lines).rstrip()
    if image_parts:
        if not picked:
            text += "\n\n（工具返回了截图，见下。）"
        return [{"type": "text", "text": text}, *image_parts]
    return text + "\n"


__all__ = [
    "OBSERVABLE_ACTIONS", "CONFIRMATION_ACTIONS",
    "PER_PAYLOAD_MAX_CHARS", "TOTAL_MAX_CHARS", "MAX_FEEDBACK_IMAGES",
    "VisualResolver",
    "is_observable", "observable_results", "render_read_results",
]
