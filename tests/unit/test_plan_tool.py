"""PlanTool 单元测试。

覆盖创建/更新/状态查询/错误路径及类方法。
"""

from __future__ import annotations

import pytest

from codepilot.tools.plan_tool import PlanTool


@pytest.fixture(autouse=True)
def _clear_plan() -> None:
    """每个测试前后清除类级别计划，避免测试间干扰。"""
    PlanTool.clear_plan()
    yield
    PlanTool.clear_plan()


# ============================================================================
# 创建计划
# ============================================================================


class TestCreatePlan:
    """PlanTool create 操作测试。"""

    async def test_create_plan_with_steps(self) -> None:
        """正常创建计划，包含步骤和标题。"""
        tool = PlanTool()
        result = await tool.execute(
            {
                "action": "create",
                "title": "重构计划",
                "steps": [
                    {"id": "s1", "description": "分析代码"},
                    {"id": "s2", "description": "编写测试"},
                ],
            }
        )
        assert "📋 计划: 重构计划" in result
        assert "[s1] 分析代码" in result
        assert "[s2] 编写测试" in result
        assert "pending" in result
        assert "进度: 0/2 完成" in result

    async def test_create_plan_without_title(self) -> None:
        """未提供 title 时使用默认标题。"""
        tool = PlanTool()
        result = await tool.execute(
            {
                "action": "create",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        assert "📋 计划: 未命名计划" in result

    async def test_create_plan_no_steps(self) -> None:
        """创建计划无步骤时返回错误。"""
        tool = PlanTool()
        result = await tool.execute({"action": "create", "title": "空计划"})
        assert "Error" in result
        assert "至少一个步骤" in result

    async def test_create_plan_empty_steps_list(self) -> None:
        """创建计划步骤为空列表时返回错误。"""
        tool = PlanTool()
        result = await tool.execute(
            {"action": "create", "title": "空计划", "steps": []}
        )
        assert "Error" in result

    async def test_create_plan_step_default_status(self) -> None:
        """步骤未指定 status 时默认为 pending。"""
        tool = PlanTool()
        result = await tool.execute(
            {
                "action": "create",
                "title": "测试",
                "steps": [
                    {"id": "s1", "description": "步骤1", "status": "completed"},
                    {"id": "s2", "description": "步骤2"},
                ],
            }
        )
        assert "进度: 1/2 完成" in result
        assert "[s2] 步骤2 (pending)" in result


# ============================================================================
# 更新计划
# ============================================================================


class TestUpdatePlan:
    """PlanTool update 操作测试。"""

    async def test_update_step_status(self) -> None:
        """正常更新步骤状态。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [
                    {"id": "s1", "description": "步骤1"},
                    {"id": "s2", "description": "步骤2"},
                ],
            }
        )
        result = await tool.execute(
            {"action": "update", "step_id": "s1", "step_status": "completed"}
        )
        assert "[s1] 步骤1 (completed)" in result
        assert "进度: 1/2 完成" in result

    async def test_update_step_to_in_progress(self) -> None:
        """更新步骤为 in_progress。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute(
            {"action": "update", "step_id": "s1", "step_status": "in_progress"}
        )
        assert "[s1] 步骤1 (in_progress)" in result

    async def test_update_step_to_failed(self) -> None:
        """更新步骤为 failed。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute(
            {"action": "update", "step_id": "s1", "step_status": "failed"}
        )
        assert "[s1] 步骤1 (failed)" in result

    async def test_update_nonexistent_step(self) -> None:
        """更新不存在的步骤返回错误。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute(
            {"action": "update", "step_id": "s99", "step_status": "completed"}
        )
        assert "Error" in result
        assert "未找到步骤" in result

    async def test_update_without_active_plan(self) -> None:
        """没有活跃计划时更新返回错误。"""
        tool = PlanTool()
        result = await tool.execute(
            {"action": "update", "step_id": "s1", "step_status": "completed"}
        )
        assert "Error" in result
        assert "没有活跃的计划" in result

    async def test_update_missing_step_id(self) -> None:
        """缺少 step_id 参数返回错误。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute(
            {"action": "update", "step_status": "completed"}
        )
        assert "Error" in result
        assert "step_id" in result

    async def test_update_missing_step_status(self) -> None:
        """缺少 step_status 参数返回错误。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute({"action": "update", "step_id": "s1"})
        assert "Error" in result
        assert "step_status" in result


# ============================================================================
# 状态查询
# ============================================================================


class TestStatus:
    """PlanTool status 操作测试。"""

    async def test_status_no_plan(self) -> None:
        """无活跃计划时返回提示。"""
        tool = PlanTool()
        result = await tool.execute({"action": "status"})
        assert "没有活跃的计划" in result

    async def test_status_with_plan(self) -> None:
        """有活跃计划时返回格式化计划。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "测试计划",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        result = await tool.execute({"action": "status"})
        assert "📋 计划: 测试计划" in result
        assert "[s1] 步骤1" in result


# ============================================================================
# 未知操作
# ============================================================================


class TestUnknownAction:
    """PlanTool 未知操作测试。"""

    async def test_unknown_action(self) -> None:
        """未知操作返回错误。"""
        tool = PlanTool()
        result = await tool.execute({"action": "delete"})
        assert "Error" in result
        assert "未知操作" in result


# ============================================================================
# 类方法
# ============================================================================


class TestClassMethods:
    """PlanTool 类方法测试。"""

    async def test_get_current_plan_none(self) -> None:
        """无计划时 get_current_plan 返回 None。"""
        assert PlanTool.get_current_plan() is None

    async def test_get_current_plan_after_create(self) -> None:
        """创建计划后 get_current_plan 返回计划字典。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "测试",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        plan = PlanTool.get_current_plan()
        assert plan is not None
        assert plan["title"] == "测试"
        assert len(plan["steps"]) == 1

    async def test_clear_plan(self) -> None:
        """clear_plan 清除当前计划。"""
        tool = PlanTool()
        await tool.execute(
            {
                "action": "create",
                "title": "测试",
                "steps": [{"id": "s1", "description": "步骤1"}],
            }
        )
        assert PlanTool.get_current_plan() is not None
        PlanTool.clear_plan()
        assert PlanTool.get_current_plan() is None
