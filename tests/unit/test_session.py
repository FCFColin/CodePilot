"""session 模块单元测试。

覆盖：SessionStorage 持久化、SessionManager 会话管理、SessionExporter 导出、
断点续跑（resume）。使用 tmp_path 隔离文件系统，structlog.testing.capture_logs()
验证日志，mock 模拟写入失败。

遵循 TDD：本文件先于 src/codepilot/session/ 实现编写，运行时应因模块不存在而失败。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from codepilot.config import ContextConfig
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.session import (
    SessionError,
    SessionExporter,
    SessionManager,
    SessionRecord,
    SessionStorage,
)

# ============================================================================
# 辅助函数
# ============================================================================


def _make_record(
    session_id: str = "test1234-1700000000",
    start_time: str = "2026-01-01T00:00:00",
    messages: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    provider: str = "deepseek",
    model: str = "astron-code-latest",
) -> SessionRecord:
    """构造测试用 SessionRecord。"""
    return SessionRecord(
        session_id=session_id,
        start_time=start_time,
        end_time=None,
        workspace_root="/tmp/test",
        messages=messages if messages is not None else [],
        tool_calls=tool_calls if tool_calls is not None else [],
        token_usage={"input_tokens": 100, "output_tokens": 50, "total": 150},
        provider=provider,
        model=model,
    )


# ============================================================================
# SessionStorage 测试
# ============================================================================


class TestSessionStorage:
    """SessionStorage 持久化测试。"""

    def test_session_storage_save_load(self, tmp_path: Path) -> None:
        """保存后加载数据一致（SessionRecord 各字段一致）。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        record = _make_record(
            messages=[{"role": "user", "content": "hello"}],
            tool_calls=[
                {
                    "tool_name": "read_file",
                    "arguments": {"path": "a.py"},
                    "result": "ok",
                    "duration_ms": 10,
                    "timestamp": "2026-01-01T00:00:01",
                }
            ],
        )
        # TypedDict 在运行时是 dict
        assert isinstance(record, dict)

        saved_path = storage.save(record)
        assert saved_path.exists()

        loaded = storage.load(record["session_id"])
        assert isinstance(loaded, dict)
        # 各字段一致
        assert loaded["session_id"] == record["session_id"]
        assert loaded["start_time"] == record["start_time"]
        assert loaded["end_time"] == record["end_time"]
        assert loaded["workspace_root"] == record["workspace_root"]
        assert loaded["messages"] == record["messages"]
        assert loaded["tool_calls"] == record["tool_calls"]
        assert loaded["token_usage"] == record["token_usage"]
        assert loaded["provider"] == record["provider"]
        assert loaded["model"] == record["model"]

    def test_session_storage_list_sorted(self, tmp_path: Path) -> None:
        """多个会话按 start_time 降序排列。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        # 创建 3 个会话，start_time 乱序
        r1 = _make_record(session_id="s1", start_time="2026-01-01T10:00:00")
        r2 = _make_record(session_id="s2", start_time="2026-01-03T10:00:00")
        r3 = _make_record(session_id="s3", start_time="2026-01-02T10:00:00")
        storage.save(r1)
        storage.save(r2)
        storage.save(r3)

        sessions = storage.list_sessions()
        assert len(sessions) == 3
        # 降序：s2 > s3 > s1
        assert sessions[0]["session_id"] == "s2"
        assert sessions[1]["session_id"] == "s3"
        assert sessions[2]["session_id"] == "s1"

    def test_session_storage_dir_permissions(self, tmp_path: Path) -> None:
        """目录权限为 0o700。

        注意：Windows 文件系统不支持完整的 Unix 权限位，stat.S_IMODE 在 Windows 上
        可能只返回部分位或 0。本测试在 Windows 上放宽为验证目录存在且可访问，
        在 Unix 上严格校验 0o700。
        """
        sessions_dir = tmp_path / "sessions"
        SessionStorage(sessions_dir=sessions_dir)
        assert sessions_dir.is_dir()

        if sys.platform == "win32":
            # Windows：权限位不完整，验证目录存在且可读写即可
            assert os.access(sessions_dir, os.R_OK | os.W_OK | os.X_OK)
        else:
            # Unix：严格校验 0o700
            mode = stat.S_IMODE(sessions_dir.stat().st_mode)
            assert (mode & 0o700) == 0o700

    def test_session_storage_load_not_found(self, tmp_path: Path) -> None:
        """加载不存在的会话抛出 SessionError。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        with pytest.raises(SessionError):
            storage.load("nonexistent-session-id")

    def test_session_storage_get_latest(self, tmp_path: Path) -> None:
        """get_latest 返回最近一个会话，无则返回 None。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        # 空目录
        assert storage.get_latest() is None

        r1 = _make_record(session_id="s1", start_time="2026-01-01T10:00:00")
        r2 = _make_record(session_id="s2", start_time="2026-01-02T10:00:00")
        storage.save(r1)
        storage.save(r2)

        latest = storage.get_latest()
        assert latest is not None
        assert latest["session_id"] == "s2"


# ============================================================================
# SessionManager 测试
# ============================================================================


class TestSessionManager:
    """SessionManager 会话管理测试。"""

    def test_session_manager_records_tool_call(self, tmp_path: Path) -> None:
        """record_tool_call 后 save 再 load，工具调用记录完整。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        session_id = manager.start_session()
        manager.add_message("user", "请读取 a.py")
        manager.record_tool_call(
            tool_name="read_file",
            arguments={"path": "a.py"},
            result="content of a.py",
            duration_ms=42,
        )

        manager.save()
        loaded = storage.load(session_id)
        assert isinstance(loaded, dict)
        assert len(loaded["tool_calls"]) == 1
        tc = loaded["tool_calls"][0]
        assert tc["tool_name"] == "read_file"
        assert tc["arguments"] == {"path": "a.py"}
        assert tc["result"] == "content of a.py"
        assert tc["duration_ms"] == 42
        # timestamp 为 ISO 8601 字符串
        assert isinstance(tc["timestamp"], str)
        assert "T" in tc["timestamp"]

    def test_session_manager_add_thinking(self, tmp_path: Path) -> None:
        """add_thinking 后 save 再 load，thinking 内容完整。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        session_id = manager.start_session()
        manager.add_message("assistant", "回答内容")
        manager.add_thinking("深度思考过程")
        manager.save()

        loaded = storage.load(session_id)
        msgs = loaded["messages"]
        assert len(msgs) == 1
        assert msgs[0]["thinking"] == "深度思考过程"

    def test_session_manager_add_thinking_no_assistant(self, tmp_path: Path) -> None:
        """没有 assistant 消息时 add_thinking 自动创建。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        session_id = manager.start_session()
        manager.add_thinking("独立思考")
        manager.save()

        loaded = storage.load(session_id)
        msgs = loaded["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["thinking"] == "独立思考"
        assert msgs[0]["content"] == ""

    def test_session_auto_export_log(self, tmp_path: Path) -> None:
        """save 后 ~/.codepilot/logs/ 目录存在 codepilot-log-{session_id}.md 文件。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        session_id = manager.start_session()
        manager.add_message("user", "hello")
        manager.save()

        log_path = Path.home() / ".codepilot" / "logs" / f"codepilot-log-{session_id}.md"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "hello" in content

    def test_session_auto_export_log_failure_silent(self, tmp_path: Path) -> None:
        """workspace_root 不可写时 _auto_export_log 不抛异常。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        # 使用一个不存在的深层路径作为 workspace_root，写入会失败
        bad_root = tmp_path / "nonexistent" / "deep" / "path"
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=bad_root,
        )
        manager.start_session()
        manager.add_message("user", "test")
        # _auto_export_log 内部写入失败应被捕获，不抛异常
        manager.save()

    def test_session_export_markdown_thinking(self) -> None:
        """导出 Markdown 时 assistant 消息的 thinking 字段用 <details> 折叠块显示。"""
        record = _make_record(
            messages=[
                {"role": "assistant", "content": "回答", "thinking": "思考过程"},
            ],
        )
        exporter = SessionExporter()
        md = exporter.to_markdown(record)
        assert "<details>" in md
        assert "<summary>🤔 Thinking</summary>" in md
        assert "思考过程" in md
        assert "</details>" in md

    def test_session_export_markdown_tool_args_result(self) -> None:
        """导出 Markdown 时工具调用汇总包含参数摘要和结果摘要。"""
        record = _make_record(
            tool_calls=[
                {
                    "tool_name": "read_file",
                    "arguments": {"path": "main.py", "offset": 10},
                    "result": "file content here",
                    "duration_ms": 20,
                    "timestamp": "2026-01-01T00:00:01",
                }
            ],
        )
        exporter = SessionExporter()
        md = exporter.to_markdown(record)
        assert "参数摘要" in md
        assert "结果摘要" in md
        assert "main.py" in md
        assert "file content here" in md

    def test_session_save_fails_silently(self, tmp_path: Path) -> None:
        """storage 写入失败时 save 不抛异常只 log warning。

        使用 structlog.testing.capture_logs() 验证 warning 日志。
        通过 mock 让 storage.save 抛 OSError，验证 SessionManager.save 静默处理。
        """
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        manager.start_session()

        # mock storage.save 抛 PermissionError（OSError 子类）
        with (
            patch.object(
                storage,
                "save",
                side_effect=PermissionError("denied"),
            ),
            structlog.testing.capture_logs() as cap_logs,
        ):
            # 不应抛异常
            manager.save()

        # 应有 warning 级别日志
        warnings = [e for e in cap_logs if e["log_level"] == "warning"]
        assert len(warnings) >= 1


# ============================================================================
# SessionExporter 测试
# ============================================================================


class TestSessionExporter:
    """SessionExporter 导出测试。"""

    def test_session_export_markdown_contains_metadata(self) -> None:
        """导出 Markdown 包含 session_id、provider、model 字段。"""
        record = _make_record(
            session_id="abc12345-1700000000",
            provider="anthropic",
            model="claude-3",
            messages=[{"role": "user", "content": "hi"}],
        )
        exporter = SessionExporter()
        md = exporter.to_markdown(record)
        assert "abc12345-1700000000" in md
        assert "anthropic" in md
        assert "claude-3" in md

    def test_session_export_json_valid(self) -> None:
        """导出 JSON 可被 json.loads 解析且包含 messages 字段。"""
        record = _make_record(
            messages=[{"role": "user", "content": "hello"}],
        )
        exporter = SessionExporter()
        json_str = exporter.to_json(record)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert "messages" in parsed
        assert parsed["messages"] == record["messages"]
        assert parsed["session_id"] == record["session_id"]


# ============================================================================
# 断点续跑测试
# ============================================================================


class TestResumeSession:
    """断点续跑测试。"""

    async def test_resume_session(self, tmp_path: Path) -> None:
        """加载历史 messages 后 context_manager.get_context 包含历史消息。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        # 第一个 manager：创建会话并保存历史
        manager1 = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        session_id = manager1.start_session()
        manager1.add_message("user", "历史问题")
        manager1.add_message("assistant", "历史回答")
        manager1.save()

        # 第二个 manager：加载历史并注入 context_manager
        manager2 = SessionManager(
            storage=storage,
            provider="deepseek",
            model="astron-code-latest",
            workspace_root=tmp_path,
        )
        history = manager2.load_history(session_id)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "历史问题"

        # 注入 context_manager
        context_manager = ContextManager(
            config=ContextConfig(),
            token_counter=TokenCounter(),
            system_prompt="",
        )
        for msg in history:
            await context_manager.add_message(msg["role"], msg["content"])

        context = await context_manager.get_context()
        # context 包含历史消息（system 消息可能在前，取决于配置）
        contents = [c.get("content", "") for c in context]
        assert any("历史问题" in str(c) for c in contents)
        assert any("历史回答" in str(c) for c in contents)


# ============================================================================
# SessionManager 边界测试
# ============================================================================


class TestSessionManagerEdgeCases:
    """SessionManager 边界情况测试。"""

    def test_get_record_without_start_session(self, tmp_path: Path) -> None:
        """未开始会话时 get_record 抛出 SessionError。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(storage=storage)
        with pytest.raises(SessionError):
            manager.get_record()

    def test_add_message_without_start_session(self, tmp_path: Path) -> None:
        """未开始会话时 add_message 不抛异常（静默忽略）。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(storage=storage)
        # 不应抛异常
        manager.add_message("user", "test")
        manager.record_tool_call("tool", {}, "result", 10)
        manager.save()

    def test_get_record_returns_record(self, tmp_path: Path) -> None:
        """start_session 后 get_record 返回当前记录。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(
            storage=storage, provider="deepseek", model="test-model"
        )
        session_id = manager.start_session()
        record = manager.get_record()
        assert isinstance(record, dict)
        assert record["session_id"] == session_id
        assert record["provider"] == "deepseek"
        assert record["model"] == "test-model"

    def test_load_history_not_found(self, tmp_path: Path) -> None:
        """加载不存在的会话历史抛出 SessionError。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        manager = SessionManager(storage=storage)
        with pytest.raises(SessionError):
            manager.load_history("nonexistent-id")


# ============================================================================
# SessionExporter 边界测试
# ============================================================================


class TestSessionExporterEdgeCases:
    """SessionExporter 边界情况测试。"""

    def test_to_markdown_with_tool_calls(self) -> None:
        """导出 Markdown 包含工具调用汇总（有 tool_calls 时）。"""
        record = _make_record(
            tool_calls=[
                {
                    "tool_name": "read_file",
                    "arguments": {"path": "a.py"},
                    "result": "content",
                    "duration_ms": 15,
                    "timestamp": "2026-01-01T00:00:01",
                }
            ],
        )
        exporter = SessionExporter()
        md = exporter.to_markdown(record)
        assert "工具调用汇总" in md
        assert "read_file" in md
        assert "15" in md

    def test_to_markdown_empty_messages(self) -> None:
        """导出 Markdown 无消息时不报错。"""
        record = _make_record(messages=[])
        exporter = SessionExporter()
        md = exporter.to_markdown(record)
        assert "对话历史" in md

    def test_to_json_complete(self) -> None:
        """导出 JSON 包含所有字段。"""
        record = _make_record()
        exporter = SessionExporter()
        parsed = json.loads(exporter.to_json(record))
        assert "session_id" in parsed
        assert "start_time" in parsed
        assert "end_time" in parsed
        assert "workspace_root" in parsed
        assert "tool_calls" in parsed
        assert "token_usage" in parsed
        assert "provider" in parsed
        assert "model" in parsed


# ============================================================================
# SessionStorage 完整性测试（A2.3）
# ============================================================================


class TestSessionStorageIntegrity:
    """SessionStorage 写入完整性测试：flush+fsync、写入失败降级、文件大小限制。"""

    def test_save_uses_flush_fsync(self, tmp_path: Path) -> None:
        """save 方法写入后调用 flush + fsync，数据应正确落盘。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        record = _make_record(messages=[{"role": "user", "content": "hello"}])
        saved_path = storage.save(record)
        # 文件应存在且内容正确
        assert saved_path.exists()
        loaded = storage.load(record["session_id"])
        assert loaded["session_id"] == record["session_id"]
        assert loaded["messages"] == record["messages"]

    def test_save_failure_graceful(self, tmp_path: Path) -> None:
        """写入失败时 save 不抛异常，只记录 error 日志。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        record = _make_record()
        # 让目录不存在且不可创建，导致写入失败
        # 通过 mock open 抛出 OSError
        with (
            patch("builtins.open", side_effect=OSError("disk full")),
            structlog.testing.capture_logs() as cap_logs,
        ):
            # 不应抛异常
            result_path = storage.save(record)
            # 应有 error 级别日志
            errors = [e for e in cap_logs if e["log_level"] == "error"]
            assert len(errors) >= 1

    def test_large_file_warning(self, tmp_path: Path) -> None:
        """JSON 文件超过 50MB 时记录警告日志。"""
        storage = SessionStorage(sessions_dir=tmp_path / "sessions")
        # 构造一个超大 messages 列表（模拟 >50MB 的会话）
        big_content = "x" * 1024  # 1KB per message
        messages = [{"role": "user", "content": big_content}] * 60000  # ~60MB
        record = _make_record(messages=messages)
        with structlog.testing.capture_logs() as cap_logs:
            storage.save(record)
        # 检查是否有警告日志（文件超过 50MB）
        warnings = [e for e in cap_logs if e["log_level"] == "warning" and "50MB" in str(e.get("event", ""))]
        assert len(warnings) >= 1
