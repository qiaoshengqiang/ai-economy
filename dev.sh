#!/usr/bin/env bash
# ============================================================
# AI Economy OS —— 开发辅助脚本
# ------------------------------------------------------------
# 为什么需要它？
#   项目跑在 .venv 虚拟环境里。如果忘了先 `source .venv/bin/activate`，
#   就会用到系统那个没装 pytest 的 Python，报 "No module named pytest"。
#
#   这个脚本直接调用 .venv 里的 Python，所以你永远不会搞错。
#
# 用法：
#   ./dev.sh test          跑全部测试
#   ./dev.sh test -v       跑测试（详细输出）
#   ./dev.sh run           启动 AI Economy OS
#   ./dev.sh clean         删掉数据库重来（会重新发钱）
#   ./dev.sh shell         进入虚拟环境的交互式 Python
#   ./dev.sh doctor        检查环境是否正常
#
#   ./dev.sh test-real deepseek     真实调用 DeepSeek API（需要 Key，会花钱）
#   ./dev.sh test-real doubao       真实调用豆包 API
#   ./dev.sh test-real all          两家的真实测试都跑
#
#   ---- 下面这些直接透传给 main.py ----
#   ./dev.sh demo                        Step 2 完整 LLM 链路演示
#   ./dev.sh ask "你好"                   问 Agent A 一句话
#   ./dev.sh providers                   看有哪些 LLM provider
#   ./dev.sh communication-demo          Step 3 Agent 间通信演示
#   ./dev.sh communication-log           Step 3.5 查询通信日志
#   ./dev.sh task-demo                   Step 4A Task Core 演示
#   ./dev.sh task-log                    查看任务
#   ./dev.sh capability-demo             Step 4B 能力发现与自主委派演示
#   ./dev.sh negotiation-demo            Step 4C 自主议价演示
#   ./dev.sh task-4d-demo                Step 4D 执行 / 提交 / 验收演示
#   ./dev.sh reliability-demo            Step 4D.1 可靠性 / 并发加固演示
#   ./dev.sh reliability-multiprocess-demo  Workspace Phase 0 多进程 / 租约演示
#   ./dev.sh communication-log --traces  列出最近的通信链路
#   ./dev.sh balances                    查看余额
#   ./dev.sh transfer agent_a agent_b 20 让 A 付 20 AI 给 B
#   ./dev.sh ledger                      查看交易流水
#   ./dev.sh audit                       对账
#   ./dev.sh --help                      查看全部命令
# ============================================================

set -euo pipefail

# 切到脚本所在的目录，这样从任何地方调用都行
cd "$(dirname "$0")"

VENV_PYTHON=".venv/bin/python"

# ------------------------------------------------------------
# 检查虚拟环境是否存在
# ------------------------------------------------------------
if [ ! -x "$VENV_PYTHON" ]; then
    echo "❌ 找不到虚拟环境：$VENV_PYTHON"
    echo
    echo "请先创建它："
    echo "    /opt/homebrew/bin/python3.13 -m venv .venv"
    echo "    ./.venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

COMMAND="${1:-help}"
shift || true

case "$COMMAND" in
    test)
        # 默认排除 real_api 标记的测试 ——
        # 它们需要 API Key 和网络，而且每跑一次都真的花钱。
        # 想跑请用 ./dev.sh test-real
        echo "▶ 运行测试（不含需要真实 API Key 的测试）..."
        "$VENV_PYTHON" -m pytest -m "not real_api" "$@"
        ;;

    test-real)
        # 只跑真实 API 集成测试。
        # 没有 Key 时会显示 "Skipped: DEEPSEEK_API_KEY not configured"，
        # 而不是失败 —— 没配 Key 的人不该看到一片红。
        TARGET="${1:-deepseek}"
        shift || true

        echo "▶ 真实 API 集成测试（provider=$TARGET）"
        echo "  注意：这会真的调用外部 API，可能产生费用。"
        echo

        case "$TARGET" in
            deepseek|doubao)
                # Key 优先从进程环境拿；没有的话 python 那边会读 .env
                KEY_VAR="$(echo "$TARGET" | tr '[:lower:]' '[:upper:]')_API_KEY"
                if [ -z "$(eval echo \"\${$KEY_VAR:-}\")" ]; then
                    echo "  环境变量 $KEY_VAR 当前未设置；"
                    echo "  如果 .env 里配了，下面的测试仍然能跑。"
                    echo
                fi
                # -k 只选中该 provider 的参数化用例
                "$VENV_PYTHON" -m pytest tests/test_real_api.py -m real_api \
                    -v -rs -k "$TARGET" "$@"
                ;;
            all)
                "$VENV_PYTHON" -m pytest tests/test_real_api.py -m real_api -v -rs "$@"
                ;;
            *)
                echo "  ❌ 不认识的目标：$TARGET"
                echo "     支持：deepseek / doubao / all"
                exit 2
                ;;
        esac
        ;;

    run)
        echo "▶ 启动 AI Economy OS..."
        "$VENV_PYTHON" main.py "$@"
        ;;

    clean)
        echo "▶ 清除运行时数据（数据库 / 日志）..."
        rm -f data/economy.db data/economy.db-wal data/economy.db-shm
        rm -f logs/*.log
        echo "✅ 已清除。下次运行会重新创建 Agent 并重新发钱。"
        ;;

    shell)
        exec "$VENV_PYTHON"
        ;;

    doctor)
        echo "▶ 环境检查"
        echo
        echo "Python 版本："
        "$VENV_PYTHON" --version
        echo
        echo "解释器路径（必须是 .venv 里的）："
        "$VENV_PYTHON" -c "import sys; print('   ', sys.executable)"
        echo
        echo "关键依赖："
        for pkg in pytest yaml dotenv; do
            if "$VENV_PYTHON" -c "import $pkg" 2>/dev/null; then
                echo "   ✅ $pkg"
            else
                echo "   ❌ $pkg  (缺失)"
            fi
        done
        echo
        echo "项目目录："
        for d in core agents storage config tests; do
            if [ -d "$d" ]; then echo "   ✅ $d/"; else echo "   ❌ $d/"; fi
        done
        echo
        echo "配置文件："
        for f in config/config.yaml agents/agent_a/config.yaml agents/agent_b/config.yaml; do
            if [ -f "$f" ]; then echo "   ✅ $f"; else echo "   ❌ $f"; fi
        done
        echo
        echo "数据库："
        if [ -f data/economy.db ]; then
            echo "   ✅ data/economy.db 已存在"
        else
            echo "   ·  data/economy.db 还不存在（首次运行时会创建）"
        fi
        ;;

    help|-h|--help|"")
        echo "AI Economy OS —— 开发辅助脚本"
        echo
        echo "环境 / 开发命令："
        echo "    ./dev.sh test [pytest 参数]    跑测试"
        echo "    ./dev.sh run  [main.py 参数]   启动系统"
        echo "    ./dev.sh clean                 清除数据库和日志"
        echo "    ./dev.sh shell                 进入交互式 Python"
        echo "    ./dev.sh doctor                检查环境"
        echo "    ./dev.sh test-real deepseek    真实 API 集成测试（需 Key，会花钱）"
        echo
        echo "Agent 通信（透传给 main.py）："
        echo "    ./dev.sh communication-demo                Step 3 通信链路演示"
        echo "    ./dev.sh communication-log                 看最近的通信消息"
        echo "    ./dev.sh communication-log --traces        列出最近的链路"
        echo "    ./dev.sh communication-log --trace trc_x   看一条链路的全部消息"
        echo
        echo "Task（透传给 main.py）："
        echo "    ./dev.sh task-demo                         Step 4A Task Core 演示"
        echo "    ./dev.sh task-log                          看任务列表"
        echo "    ./dev.sh task-log <task_id>                看某个任务详情"
        echo "    ./dev.sh capability-demo                   能力发现与自主委派演示"
        echo "    ./dev.sh negotiation-demo                  自主议价演示（Step 4C）"
        echo "    ./dev.sh task-4d-demo                      执行/提交/验收演示（Step 4D）"
        echo "    ./dev.sh reliability-demo                  可靠性/并发加固演示（Step 4D.1）"
        echo "    ./dev.sh reliability-multiprocess-demo     多进程/认领租约演示（Phase 0）"
        echo
        echo "LLM（透传给 main.py）："
        echo "    ./dev.sh demo [--prompt 文字] [--agent a]  Step 2 链路演示"
        echo "    ./dev.sh ask \"文字\" [--agent b]            问某个 Agent"
        echo "    ./dev.sh providers                        看 provider 列表"
        echo
        echo "经济操作（透传给 main.py）："
        echo "    ./dev.sh balances                          查看所有 Agent 余额"
        echo "    ./dev.sh transfer <付款方> <收款方> <金额>   转钱"
        echo "    ./dev.sh ledger [--agent a]                查看交易流水"
        echo "    ./dev.sh audit                             对账"
        echo
        echo "例子："
        echo "    ./dev.sh test -v                          详细测试输出"
        echo "    ./dev.sh test-real deepseek              真实调用 DeepSeek"
        echo "    ./dev.sh demo                              完整链路演示"
        echo "    ./dev.sh communication-demo                Agent 间通信演示"
        echo "    ./dev.sh communication-log --traces        看通信链路"
        echo "    ./dev.sh task-demo                         Task Core 演示"
        echo "    ./dev.sh capability-demo                   能力发现演示"
        echo "    ./dev.sh negotiation-demo                  自主议价演示"
        echo "    ./dev.sh ask \"你好\"                        问 Agent A"
        echo "    ./dev.sh transfer agent_a agent_b 20       A 付 20 AI 给 B"
        echo "    ./dev.sh transfer a b 20 --reason 任务定金  简写 + 原因"
        echo "    ./dev.sh run --quiet                      简洁启动输出"
        ;;

    *)
        # 其它命令一律透传给 main.py。
        # 这样 main.py 以后新增命令，dev.sh 不需要跟着改。
        exec "$VENV_PYTHON" main.py "$COMMAND" "$@"
        ;;
esac
