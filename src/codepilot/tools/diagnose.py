"""错误诊断工具。

收集项目错误信息，运行 linter、读取 traceback、检查文件状态。
所有 I/O 异常静默处理，不抛出。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import structlog

from codepilot.tools.registry import BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)

# 最大 traceback 行数
_MAX_TRACEBACK_LINES = 50
# Linter 超时
_LINTER_TIMEOUT = 30


class DiagnoseTool(BaseTool):
    """收集项目错误信息，运行 linter 检查代码问题，读取 traceback 文件，
    检查文件状态。"""

    name = "diagnose"
    description = (
        "诊断项目错误。运行 linter 检查代码问题，读取 traceback 文件，"
        "检查文件状态。返回结构化诊断报告。"
    )

    def get_parameters(self) -> dict[str, Any]:
        """返回参数 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "error_description": {
                    "type": "string",
                    "description": "错误描述（如报错信息、异常类型等）",
                },
                "file_path": {
                    "type": "string",
                    "description": "相关文件路径（可选）",
                },
                "run_linter": {
                    "type": "boolean",
                    "description": "是否运行 linter（默认 true）",
                },
            },
            "required": ["error_description"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: Any = None,
    ) -> str:
        """执行错误诊断。"""
        error_desc = arguments.get("error_description", "")
        file_path = arguments.get("file_path", "")
        run_linter = arguments.get("run_linter", True)

        logger.info(
            "diagnose 开始诊断",
            error_desc=error_desc[:100],
            file_path=file_path,
            run_linter=run_linter,
        )

        sections: list[str] = []

        # 1. 错误描述
        sections.append(f"## 错误描述\n{error_desc}")

        # 2. 文件状态检查
        if file_path:
            sections.append(self._check_file_status(file_path))

        # 3. 运行 linter
        if run_linter:
            linter_result = await self._run_linter(file_path)
            sections.append(linter_result)

        # 4. 检查常见问题
        sections.append(self._check_common_issues(file_path))

        return "\n\n".join(sections)

    def _check_file_status(self, file_path: str) -> str:
        """检查文件状态。"""
        lines = [f"## 文件状态: {file_path}"]

        path = Path(file_path)
        if not path.exists():
            lines.append("❌ 文件不存在")
            return "\n".join(lines)

        lines.append("✅ 文件存在")
        lines.append(f"大小: {path.stat().st_size} bytes")
        lines.append(f"扩展名: {path.suffix}")

        # 读取前 20 行
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            file_lines = content.splitlines()
            lines.append(f"总行数: {len(file_lines)}")
            if file_lines:
                preview = "\n".join(
                    f"  {i + 1}: {line}" for i, line in enumerate(file_lines[:20])
                )
                lines.append(f"前 20 行:\n{preview}")
        except Exception as e:
            lines.append(f"读取失败: {e}")

        return "\n".join(lines)

    async def _run_linter(self, file_path: str) -> str:
        """运行 linter。"""
        lines = ["## Linter 检查"]

        # 检测项目类型并运行对应 linter
        linters: list[tuple[str, list[str]]] = []

        # Python: ruff
        if file_path.endswith(".py") or Path("pyproject.toml").exists():
            cmd = ["ruff", "check"]
            if file_path and file_path.endswith(".py"):
                cmd.append(file_path)
            linters.append(("ruff", cmd))

        # 如果没有检测到 linter，跳过
        if not linters:
            lines.append("未检测到适用的 linter")
            return "\n".join(lines)

        for name, cmd in linters:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_LINTER_TIMEOUT
                )
                output = stdout.decode("utf-8", errors="replace").strip()
                error_output = stderr.decode("utf-8", errors="replace").strip()

                if proc.returncode == 0:
                    lines.append(f"✅ {name}: 无问题")
                    logger.debug("diagnose linter 通过", linter=name)
                else:
                    lines.append(f"❌ {name} (exit code {proc.returncode}):")
                    if output:
                        lines.append(output[:2000])
                    if error_output:
                        lines.append(f"stderr: {error_output[:1000]}")
                    logger.warning(
                        "diagnose linter 发现问题",
                        linter=name,
                        exit_code=proc.returncode,
                    )
            except TimeoutError:
                lines.append(f"⏰ {name}: 超时")
                logger.warning(
                    "diagnose linter 超时",
                    linter=name,
                    timeout=_LINTER_TIMEOUT,
                )
            except FileNotFoundError:
                lines.append(f"⚠️ {name}: 未安装")
                logger.debug("diagnose linter 未安装", linter=name)
            except Exception as e:
                lines.append(f"❌ {name}: 执行失败 - {e}")
                logger.error(
                    "diagnose linter 执行失败",
                    linter=name,
                    error=str(e)[:200],
                )

        return "\n".join(lines)

    def _check_common_issues(self, file_path: str) -> str:
        """检查常见问题。"""
        lines = ["## 常见问题检查"]

        # 检查编码问题
        if file_path and Path(file_path).exists():
            try:
                content = Path(file_path).read_bytes()
                # 检查 BOM
                if content.startswith(b"\xef\xbb\xbf"):
                    lines.append("⚠️ 文件包含 UTF-8 BOM")
                # 检查行尾
                if b"\r\n" in content:
                    lines.append("ℹ️ 文件使用 CRLF 行尾（Windows）")
                elif b"\r" in content:
                    lines.append("⚠️ 文件使用旧式 Mac 行尾（CR）")
                # 检查空文件
                if len(content) == 0:
                    lines.append("⚠️ 文件为空")
            except Exception:
                pass

        # 检查磁盘空间
        try:
            usage = shutil.disk_usage(".")
            free_gb = usage.free / (1024**3)
            if free_gb < 1:
                lines.append(f"⚠️ 磁盘空间不足: {free_gb:.1f} GB 可用")
            else:
                lines.append(f"✅ 磁盘空间: {free_gb:.1f} GB 可用")
        except Exception:
            pass

        return "\n".join(lines)


__all__ = ["DiagnoseTool"]
