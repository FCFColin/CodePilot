"""Git 仓库管理器。

通过 subprocess 调用真实 git 命令实现仓库检测、自动提交、撤销、脏文件检测。
所有操作在非 git 仓库中静默失败返回 None/False/[]，禁止抛异常。
禁止使用 GitPython 库。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# codepilot 提交信息前缀
_CODEPILOT_PREFIX = "[codepilot]"


class GitManager:
    """Git 仓库管理器，封装常用 git 操作。

    所有 git 操作通过 subprocess.run 调用真实 git 命令。
    在非 git 仓库中所有操作静默失败，返回 None/False/[]，不抛异常。

    Attributes:
        workspace_root: 工作区根目录。
        repo: 检测到的 git 仓库根目录 Path，非 git 仓库时为 None。
    """

    def __init__(self, workspace_root: Path) -> None:
        """初始化 GitManager，检测是否在 git 仓库中。

        Args:
            workspace_root: 工作区根目录路径。
        """
        self.workspace_root: Path = workspace_root
        self.repo: Path | None = self._detect_repo()
        if self.repo is not None:
            logger.debug("检测到 git 仓库", repo=str(self.repo))

    def _detect_repo(self) -> Path | None:
        """检测 workspace_root 是否在 git 仓库中。

        使用 git rev-parse --show-toplevel 获取仓库根目录。
        静默失败（捕获所有异常，返回 None）。

        Returns:
            git 仓库根目录 Path，非 git 仓库或失败时返回 None。
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None
            toplevel = result.stdout.strip()
            if not toplevel:
                return None
            return Path(toplevel)
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("git 仓库检测失败", error=str(e))
            return None

    def is_git_repo(self) -> bool:
        """返回当前工作区是否在 git 仓库中。"""
        return self.repo is not None

    def auto_commit(self, message: str, paths: list[Path]) -> str | None:
        """自动提交指定路径的修改。

        执行 git add 指定路径，然后 git commit。
        提交信息前缀强制为 [codepilot]，若 message 不以此开头则自动添加。
        所有异常静默处理返回 None。

        Args:
            message: 提交信息（不带 [codepilot] 前缀时自动添加）。
            paths: 已修改的文件路径列表。

        Returns:
            提交 hash 前 8 位字符串，失败或非 git 仓库时返回 None。
        """
        if self.repo is None:
            logger.debug("非 git 仓库，跳过 auto_commit")
            return None

        if not paths:
            logger.debug("无修改路径，跳过 auto_commit")
            return None

        # 强制添加 [codepilot] 前缀
        if not message.startswith(_CODEPILOT_PREFIX):
            commit_message = f"{_CODEPILOT_PREFIX} {message}"
        else:
            commit_message = message

        try:
            # git add 指定路径
            str_paths = [str(p) for p in paths]
            add_result = subprocess.run(
                ["git", "add", *str_paths],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if add_result.returncode != 0:
                logger.warning(
                    "git add 失败",
                    paths=str_paths,
                    stderr=add_result.stderr,
                )
                return None

            # git commit
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if commit_result.returncode != 0:
                logger.debug(
                    "git commit 失败（可能无变更）",
                    stderr=commit_result.stderr,
                )
                return None

            # 获取提交 hash 前 8 位
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if hash_result.returncode != 0:
                logger.warning("获取 HEAD hash 失败")
                return None
            commit_hash = hash_result.stdout.strip()[:8]
            logger.info(
                "auto_commit 成功",
                hash=commit_hash,
                message=commit_message,
            )
            return commit_hash
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("auto_commit 异常", error=str(e))
            return None

    def undo_last_commit(self) -> tuple[bool, str]:
        """撤销最近一次提交（仅当它是 codepilot 提交时）。

        执行 git log --oneline -1 检查最近提交信息是否以 [codepilot] 开头，
        是则执行 git reset --soft HEAD~1 撤销提交（保留工作区变更）。

        Returns:
            (success, message) 元组。成功时 message 为撤销的提交信息，
            失败时 message 为原因说明。非 git 仓库返回 (False, ...)。
        """
        if self.repo is None:
            return False, "非 git 仓库，无法撤销提交"

        try:
            # 获取最近一次提交信息
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if log_result.returncode != 0:
                return False, "无法读取 git log（可能无提交）"

            log_line = log_result.stdout.strip()
            if not log_line:
                return False, "无提交可撤销"

            # log --oneline 格式: "<hash> <message>"
            # 提取提交信息部分
            parts = log_line.split(" ", 1)
            if len(parts) < 2:
                return False, "无法解析提交信息"
            commit_message = parts[1]

            if not commit_message.startswith(_CODEPILOT_PREFIX):
                return False, "最近一次提交不是 codepilot 提交，拒绝回滚"

            # 检查是否有父提交（是否为初始提交）
            # 通过 git rev-parse --verify HEAD~1 判断
            parent_check = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD~1"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if parent_check.returncode == 0:
                # 有父提交：使用 git reset --soft HEAD~1
                reset_result = subprocess.run(
                    ["git", "reset", "--soft", "HEAD~1"],
                    cwd=self.workspace_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                # 初始提交：使用 git update-ref -d HEAD 撤销
                reset_result = subprocess.run(
                    ["git", "update-ref", "-d", "HEAD"],
                    cwd=self.workspace_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if reset_result.returncode != 0:
                logger.warning("git reset 失败", stderr=reset_result.stderr)
                return False, f"git reset 失败: {reset_result.stderr.strip()}"

            logger.info("已撤销 codepilot 提交", message=commit_message)
            return True, commit_message
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("undo_last_commit 异常", error=str(e))
            return False, f"撤销异常: {e}"

    def get_dirty_files(self) -> list[Path]:
        """获取已修改但未提交的文件列表。

        执行 git status --porcelain 解析输出。

        Returns:
            已修改文件路径列表（相对于仓库根目录）。非 git 仓库返回空列表。
        """
        if self.repo is None:
            return []

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                logger.warning("git status 失败", stderr=result.stderr)
                return []

            dirty_files: list[Path] = []
            for line in result.stdout.splitlines():
                if not line:
                    continue
                # porcelain 格式: "XY filename" 或 "XY filename -> renamed"
                # X 为暂存区状态，Y 为工作区状态，文件名从第 3 字符开始
                file_part = line[3:]
                # 处理重命名: "old -> new" 取 new
                if " -> " in file_part:
                    file_part = file_part.split(" -> ", 1)[1]
                # 去除引号（git 对含特殊字符的文件名会加引号）
                file_part = file_part.strip().strip('"')
                if file_part:
                    dirty_files.append(Path(file_part))
            return dirty_files
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("get_dirty_files 异常", error=str(e))
            return []


__all__ = ["GitManager"]
