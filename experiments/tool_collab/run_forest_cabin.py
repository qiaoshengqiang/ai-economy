#!/usr/bin/env python3
"""
5-Agent 协作实验：沉浸式 3D 林中小屋建模演示器
=================================================

Human Command 是唯一任务输入。五个 Agent 用**最小角色脚手架**协作：

    agent_a  docs/    协调者 / 契约 / 接口
    agent_b  site/ + scene/   生产者：Three.js 场景 + 最终 index.html
    agent_c  cabin/   小屋几何与配置
    agent_d  ui/      编辑面板与控制
    agent_e  qa/      独立验收

用法：
    ./.venv/bin/python experiments/tool_collab/run_forest_cabin.py \
        --minutes 30 --session-minutes 6 --rounds 3 --budget 30
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv                                    # noqa: E402

load_dotenv(ROOT / ".env")

HUMAN_COMMAND = """# 项目任务：沉浸式 3D 林中小屋建模演示器

请制作一个高质量、具有现代设计感的 Web 3D 交互演示网页，主题为「Forest Cabin — 林中小屋」。

## 核心目标

用户可以在一个真实感较强的森林环境中，观察、旋转、缩放一座精致的林中小屋，并通过网页界面实时调整小屋的外观和配置。

这不是静态效果图，而是一个具有真实交互能力的 3D 建模演示器。

## 核心功能

1. **3D 场景**：创建森林环境，包括树木、草地、地形、岩石、天空、光照和环境氛围。
2. **小屋建模**：小屋应具有屋顶、墙体、门窗、烟囱、木质结构等细节，整体风格自然、精致。
3. **实时编辑**：用户可以切换屋顶样式、墙体材质、木材颜色、门窗配置，以及添加或移除部分装饰。
4. **场景控制**：支持鼠标拖动旋转视角、滚轮缩放、视角复位，以及不同观察角度。
5. **环境控制**：支持白天、黄昏、夜晚等场景切换，并体现相应的光照变化。
6. **界面设计**：提供现代化的编辑面板，清晰展示模型配置、材质选项和场景控制。

## 视觉要求

- 整体达到优秀商业级 Web 3D 展示网站的视觉水准。
- 采用自然、温暖、沉浸式的视觉风格。
- 小屋必须是视觉中心，森林环境用于增强空间氛围。
- 界面简洁、精致，避免传统后台管理系统的观感。
- 具有丰富的材质细节、合理的光影和空间层次。
- 动画、交互和界面反馈应当流畅自然。

## 技术要求

使用适合浏览器的 3D 技术实现，优先考虑 Three.js 或 React Three Fiber。
所有主要交互必须真实可用，不能只是视觉上模拟按钮。模型配置变化必须实时反映在 3D 场景中。
最终交付一个可以直接运行和体验的完整网页项目。"""

AGENTS = ["agent_a", "agent_b", "agent_c", "agent_d", "agent_e"]

NAMESPACES = {
    "docs": "agent_a",
    "site": "agent_b",
    "scene": "agent_b",
    "cabin": "agent_c",
    "ui": "agent_d",
    "qa": "agent_e",
}

PRODUCER = "agent_b"
ACCEPTOR = "agent_e"
OWNER = "agent_a"

DEFINITION_OF_DONE = (
    "site/index.html 存在且可在浏览器打开",
    "Three.js 场景真实渲染（canvas 有可见森林/地形/树木，不是空白）",
    "小屋包含屋顶、墙体、门窗、烟囱等细节",
    "编辑面板可切换屋顶/墙体/木材颜色/门窗配置，并实时反映到 3D",
    "支持环境切换（白天/黄昏/夜晚）并体现光照变化",
    "支持鼠标旋转/缩放/视角复位",
    "至少一次 run_tool(playwright) 实际执行浏览器验证",
    "至少一个非实现者写出独立验收结论",
)

ROLE_PROMPTS = {
    "agent_a": (
        "你是协调者/契约负责人。在 docs/ 写最小接口契约："
        "`window.ForestCabin` 暴露 setRoof/setWall/setWoodColor/setDoor/"
        "setWindow/setTime/addDecor/removeDecor/resetView/getState；"
        "明确文件边界：site/scene 归 agent_b，cabin 归 agent_c，"
        "ui 归 agent_d，qa 归 agent_e。主动发消息推动集成。"),
    "agent_b": (
        "你是生产者/集成者。写 site/index.html 与 scene/main.js："
        "用 Three.js（CDN）搭建森林、地形、树木、天空、光照和 Renderer；"
        "import ../cabin/cabin.js 与 ../ui/panel.js；把契约接口挂到 "
        "window.ForestCabin。用 run_tool(playwright) 实际验证并截图。"
        "最终由你发 SUBMIT。"),
    "agent_c": (
        "你负责 cabin/cabin.js：用 Three.js 程序化几何构建精致小屋"
        "（屋顶/墙体/门窗/烟囱/木结构），导出 createCabin 与配置应用函数；"
        "不要改 site/ 或 scene/。"),
    "agent_d": (
        "你负责 ui/panel.js：现代编辑面板（屋顶/墙体/木材颜色/门窗/"
        "装饰/环境/视角复位），真实调用 window.ForestCabin 的接口；"
        "不要改 site/ 或 scene/。"),
    "agent_e": (
        "你是独立验收人。用 run_tool(playwright) 实际打开页面，"
        "验证场景非空白、小屋可见、至少 2 个配置变化和 2 个环境变化；"
        "写 qa/review.md，按状态机发 VERIFY / COMPLETE；不实现页面。"),
}


def _parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="run_forest_cabin.py")
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--session-minutes", type=float, default=6.0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget", type=float, default=30.0)
    ap.add_argument("--cognitive-budget", type=int, default=16384)
    ap.add_argument("--db", default=None,
                    help="复用已有数据库（跨任务记忆实验）")
    ap.add_argument("--out", default=None)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    run_id = uuid.uuid4().hex[:8]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else HERE / "runs" / f"{datetime.now():%m%d-%H%M}-{run_id}")
    outdir.mkdir(parents=True, exist_ok=True)
    results = outdir / "results"
    results.mkdir(parents=True, exist_ok=True)
    db = (Path(args.db).expanduser().resolve() if args.db
          else outdir / "forest.db")

    from experiments.heterogeneous_production import production as P

    task_id = f"task_forest_{run_id}"
    ws_id = f"WS-FOREST-{run_id}"

    P.DEMAND = HUMAN_COMMAND.strip()
    P.AGENTS = list(AGENTS)
    P.OWNER = OWNER
    P.PRODUCER = PRODUCER
    P.ACCEPTOR = ACCEPTOR
    P.TASK_ID = task_id
    P.WS_ID = ws_id
    P.DB = db
    P.RESULTS = results
    P.EXPERIMENT_ID = f"forest_cabin_{run_id}"
    P.ROOT_STAGE_ID = f"stage_{task_id}_root"
    P.ARTIFACT_NAMESPACES = dict(NAMESPACES)
    P.DEFINITION_OF_DONE = DEFINITION_OF_DONE
    P.AGENT_DRIVEN_COMPLETION = True
    P.COMPLETION_QA_ARTIFACT = "qa/review.md"
    P.BUDGET_AIC = float(args.budget)
    P.TOTAL_WALL_LIMIT = max(60.0, args.minutes * 60.0)
    P.SESSION_LIMIT = max(30.0, args.session_minutes * 60.0)
    P.MAX_ROUNDS_PER_SESSION = max(1, args.rounds)
    P.COGNITIVE_BUDGET = int(args.cognitive_budget)
    P.MIN_PRODUCTIVE_CHARS_PER_SESSION = 200
    P.FINAL_WINDOW_RATIO = 0.2
    P.FINAL_WINDOW_SKIP_AGENTS = ("agent_c", "agent_d")

    import main as M
    _orig_build = M.build_runtime

    def _build_with_roles(*a, **kw):
        rt = _orig_build(*a, **kw)
        for aid, role in ROLE_PROMPTS.items():
            brain = rt.economy.agent(aid).brain
            brain.settings.system_prompt = (
                f"{brain.settings.system_prompt}\n\n{role}")
        return rt

    M.build_runtime = _build_with_roles

    print("=" * 72)
    print("  5-Agent 协作：沉浸式 3D 林中小屋建模演示器")
    print("=" * 72)
    print(f"  参与者: {', '.join(AGENTS)}")
    print(f"  模式  : GUIDED 角色脚手架")
    print(f"  分区  : " + " · ".join(f"{k}→{v}" for k, v in NAMESPACES.items()))
    print(f"  生产者: {PRODUCER}   验收人: {ACCEPTOR}")
    print(f"  墙钟  : {P.TOTAL_WALL_LIMIT/60:.1f} 分钟 "
          f"（Session {P.SESSION_LIMIT/60:.1f} 分钟，"
          f"{P.MAX_ROUNDS_PER_SESSION} 轮）")
    print(f"  预算  : {P.BUDGET_AIC} AIC")
    print(f"  时间  : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  输出  : {outdir}")
    print("=" * 72)
    print()

    try:
        rc = P.main()
    finally:
        print()
        print("=" * 72)
        print("  产物位置")
        print("=" * 72)
        print(f"  数据库   : {db}")
        print(f"  结果 JSON: {results / 'heterogeneous_production_result.json'}")
        print(f"  输出     : {outdir}")
        print("=" * 72)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
