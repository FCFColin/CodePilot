"""Repo Map 子包。

提供仓库结构摘要生成能力（可选功能）。依赖 tree-sitter-language-pack
与 networkx；未安装时 RepoMapper.is_available() 返回 False，所有
构建方法返回空字符串，不抛异常。
"""

from __future__ import annotations

from codepilot.repomap.mapper import RepoMapper

__all__ = ["RepoMapper"]
