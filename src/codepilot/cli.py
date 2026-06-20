"""CLI 入口点。

完整集成：parse_args → load_config → create_app → REPL/单次执行。
支持 Ctrl+C 优雅退出、CodePilotError 错误处理。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from codepilot import __version__
from codepilot.app import create_app
from codepilot.config import load_config
from codepilot.exceptions import CodePilotError, ConfigError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    Args:
        argv: 参数列表，默认从 sys.argv 读取。

    Returns:
        解析后的 Namespace。
    """
    parser = argparse.ArgumentParser(
        prog="codepilot",
        description="CodePilot - 终端 AI 编码智能体",
    )
    parser.add_argument("prompt", nargs="?", help="单次执行模式的提示词")
    parser.add_argument(
        "--provider", choices=["deepseek", "anthropic"], help="LLM provider"
    )
    parser.add_argument("--model", help="模型名")
    parser.add_argument("--api-key", help="API Key")
    parser.add_argument("--workspace", help="工作目录")
    parser.add_argument(
        "--no-approve", action="store_true", help="禁用审批（YOLO 模式）"
    )
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    parser.add_argument(
        "--version", action="version", version=f"CodePilot v{__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI 主入口。

    解析参数 → 加载配置 → 创建 App → 根据 prompt 决定 REPL 或单次模式。
    捕获 ConfigError 输出到 stderr 并以非零码退出；
    捕获 KeyboardInterrupt 优雅退出；
    捕获 CodePilotError 输出错误信息。

    Args:
        argv: 命令行参数列表，默认从 sys.argv 读取。
    """
    args = parse_args(argv)

    # 加载配置（fail-fast 校验 API Key）
    try:
        config = load_config(args)
    except ConfigError as e:
        sys.stderr.write(f"配置错误: {e}\n")
        sys.exit(1)

    # 创建应用容器（组装所有组件）
    try:
        app = create_app(config)
    except CodePilotError as e:
        sys.stderr.write(f"初始化失败: {e}\n")
        sys.exit(1)

    # 运行 REPL 或单次执行
    try:
        if args.prompt:
            asyncio.run(app.run_single(args.prompt))
        else:
            asyncio.run(app.run_repl())
    except KeyboardInterrupt:
        # Ctrl+C 优雅退出
        sys.exit(0)
    except CodePilotError as e:
        sys.stderr.write(f"错误: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
