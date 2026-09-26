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
        echo "▶ 运行测试..."
        "$VENV_PYTHON" -m pytest "$@"
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

    help|*)
        echo "AI Economy OS —— 开发辅助脚本"
        echo
        echo "用法："
        echo "    ./dev.sh test [pytest 参数]    跑测试"
        echo "    ./dev.sh run  [main.py 参数]   启动系统"
        echo "    ./dev.sh clean                 清除数据库和日志"
        echo "    ./dev.sh shell                 进入交互式 Python"
        echo "    ./dev.sh doctor                检查环境"
        echo
        echo "例子："
        echo "    ./dev.sh test -v                   详细测试输出"
        echo "    ./dev.sh test tests/test_ledger.py  只跑账本测试"
        echo "    ./dev.sh run --quiet               简洁启动输出"
        ;;
esac
