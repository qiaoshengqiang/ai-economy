"""
日志系统
============================================================

为什么日志在这个项目里特别重要？
    经济系统的 bug 很隐蔽。
    如果 A 少了 20 AIC 但 B 没多 20 AIC，光看最终余额是查不出来的，
    必须能顺着日志一条条追。

所以我们的目标是让日志读起来像一份「事件流水」：

    [12:01:10] Task #001 created
    [12:01:12] Agent A evaluating task
    [12:01:13] Agent A decided to outsource
    [12:01:14] Message sent A -> B
    [12:01:15] Agent B submitted bid: 20 AIC
    ...
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from core.paths import LOGS_DIR

# 全局只配置一次
_configured = False

# 自定义日志等级名称，让输出更好看
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # 青色
    "INFO": "\033[32m",      # 绿色
    "WARNING": "\033[33m",   # 黄色
    "ERROR": "\033[31m",     # 红色
    "CRITICAL": "\033[41m",  # 红底
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """
    终端彩色日志格式器。

    格式： [12:01:10] INFO  message
    """

    def __init__(self, use_color: bool = True):
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        level = record.levelname

        if self.use_color:
            color = _LEVEL_COLORS.get(level, "")
            level_str = f"{color}{level:<7}{_RESET}"
        else:
            level_str = f"{level:<7}"

        message = record.getMessage()

        # 异常信息也要打出来，否则调试时看不到堆栈
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        return f"[{time_str}] {level_str} {message}"


def setup_logging(
    level: str = "INFO",
    log_to_file: bool = False,
    log_file: Optional[Path] = None,
    force: bool = False,
) -> logging.Logger:
    """
    初始化日志系统。

    参数：
        level:       日志等级字符串
        log_to_file: 是否同时写日志文件
        force:       强制重新配置（测试时会用到）
    """
    global _configured

    logger = logging.getLogger("ai_economy")

    if _configured and not force:
        return logger

    # 清掉已有的 handler，避免重复输出
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # --- 终端输出 ---
    # 如果终端不支持颜色（比如重定向到文件），自动降级
    use_color = sys.stdout.isatty()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColorFormatter(use_color=use_color))
    logger.addHandler(console)

    # --- 文件输出（可选）---
    if log_to_file:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        target = log_file or (LOGS_DIR / "ai_economy.log")
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    取得一个 logger。

    子模块用法： logger = get_logger(__name__)
    这样日志里能看到消息来自哪个模块（DEBUG 等级时）。
    """
    if name:
        return logging.getLogger(f"ai_economy.{name}")
    return logging.getLogger("ai_economy")
