"""会话持久化子包。

提供会话管理、存储、导出能力：
- SessionStorage：JSON 文件持久化
- SessionManager：会话生命周期管理
- SessionExporter：Markdown/JSON 导出
- SessionRecord：会话记录 TypedDict
- SessionError：会话相关异常
"""

from __future__ import annotations

from codepilot.session.export import SessionExporter
from codepilot.session.manager import SessionManager
from codepilot.session.storage import (
    SessionError,
    SessionRecord,
    SessionStorage,
)

__all__ = [
    "SessionError",
    "SessionExporter",
    "SessionManager",
    "SessionRecord",
    "SessionStorage",
]
