"""执行计划工具。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from codepilot.tools.registry import BaseTool

if TYPE_CHECKING:
    from codepilot.tools.registry import ApprovalProtocol, SandboxProtocol

logger = structlog.get_logger(__name__)


class PlanTool(BaseTool):
    """创建和更新结构化执行计划。"""

    name = "plan"
    description = (
        "创建或更新执行计划。制定步骤列表，跟踪进度。适用于复杂任务的规划和执行跟踪。"
    )

    # 类级别存储当前计划
    _current_plan: dict[str, Any] | None = None

    def get_parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "status"],
                    "description": (
                        "操作类型：create=创建计划，"
                        "update=更新步骤状态，status=查看当前计划"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "计划标题（create 时必需）",
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "步骤 ID"},
                            "description": {
                                "type": "string",
                                "description": "步骤描述",
                            },
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "failed",
                                ],
                                "description": "步骤状态",
                            },
                        },
                        "required": ["id", "description"],
                    },
                    "description": "步骤列表（create 时必需）",
                },
                "step_id": {
                    "type": "string",
                    "description": "要更新的步骤 ID（update 时必需）",
                },
                "step_status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed"],
                    "description": "新状态（update 时必需）",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行计划操作。"""
        action = arguments.get("action", "")

        if action == "create":
            return self._create_plan(arguments)
        elif action == "update":
            return self._update_plan(arguments)
        elif action == "status":
            return self._get_status()
        else:
            return f"Error: 未知操作 '{action}'，可选: create, update, status"

    def _create_plan(self, arguments: dict[str, Any]) -> str:
        """创建新计划。"""
        title = arguments.get("title", "未命名计划")
        steps = arguments.get("steps", [])

        if not steps:
            return "Error: 创建计划需要至少一个步骤"

        # 初始化步骤状态
        for step in steps:
            step.setdefault("status", "pending")

        PlanTool._current_plan = {
            "title": title,
            "steps": steps,
        }

        logger.info("plan 创建计划", title=title, step_count=len(steps))

        return self._format_plan()

    def _update_plan(self, arguments: dict[str, Any]) -> str:
        """更新步骤状态。"""
        if PlanTool._current_plan is None:
            return "Error: 没有活跃的计划，请先使用 create 创建"

        step_id = arguments.get("step_id", "")
        step_status = arguments.get("step_status", "")

        if not step_id or not step_status:
            return "Error: update 需要 step_id 和 step_status 参数"

        # 查找并更新步骤
        found = False
        for step in PlanTool._current_plan["steps"]:
            if step["id"] == step_id:
                step["status"] = step_status
                found = True
                break

        if not found:
            logger.warning("plan 更新步骤未找到", step_id=step_id)
            return f"Error: 未找到步骤 '{step_id}'"

        logger.info("plan 更新步骤", step_id=step_id, step_status=step_status)

        return self._format_plan()

    def _get_status(self) -> str:
        """获取当前计划状态。"""
        if PlanTool._current_plan is None:
            return "当前没有活跃的计划"

        return self._format_plan()

    def _format_plan(self) -> str:
        """格式化计划为可读文本。"""
        if PlanTool._current_plan is None:
            return "当前没有活跃的计划"

        title = PlanTool._current_plan["title"]
        steps = PlanTool._current_plan["steps"]

        lines = [f"📋 计划: {title}", ""]

        status_icons: dict[str, str] = {
            "pending": "⬜",
            "in_progress": "🔄",
            "completed": "✅",
            "failed": "❌",
        }

        completed = sum(1 for s in steps if s["status"] == "completed")
        total = len(steps)

        for step in steps:
            icon = status_icons.get(step["status"], "⬜")
            lines.append(
                f"  {icon} [{step['id']}] {step['description']} ({step['status']})"
            )

        lines.append("")
        lines.append(f"进度: {completed}/{total} 完成")

        return "\n".join(lines)

    @classmethod
    def get_current_plan(cls) -> dict[str, Any] | None:
        """获取当前计划（供 /plan 命令使用）。"""
        return cls._current_plan

    @classmethod
    def clear_plan(cls) -> None:
        """清除当前计划。"""
        cls._current_plan = None


__all__ = ["PlanTool"]
