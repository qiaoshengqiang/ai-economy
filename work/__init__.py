"""
Work 层 —— 具体执行
====================

保留：

    Task · Workspace · Shared Context · Private Working Memory ·
    Short / Long-Term Memory · Draft · Tool Runtime

不保留：

    Agent 间消息 · 身份经济 · 钱包 / 价格 / 议价 · 生活层邀请协议

Work 层的入口是 `WorkTeamRuntime`（Solo 先实现）；Task 就是团队容器，
不再引入第二套 Team 实体。详见 `docs/architecture/LIFE_WORK_ARCHITECTURE.md`。
"""

from work.lifecycle import WorkLifecycle, WorkLifecycleError
from work.plan import WorkItem, WorkPlan, parse_work_plan
from work.roles import WorkRole, WorkResult, WorkContext
from work.runtime import SoloWorkRuntime
from work.team_runtime import TeamWorkRuntime

__all__ = [
    "WorkLifecycle", "WorkLifecycleError",
    "WorkItem", "WorkPlan", "parse_work_plan",
    "WorkRole", "WorkResult", "WorkContext",
    "SoloWorkRuntime", "TeamWorkRuntime",
]
