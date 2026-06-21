"""Git 集成子包。

提供 GitManager（仓库管理）和 CommitMessageGenerator（提交信息生成）。
"""

from codepilot.git.commit import CommitMessageGenerator
from codepilot.git.manager import GitManager

__all__ = ["GitManager", "CommitMessageGenerator"]
