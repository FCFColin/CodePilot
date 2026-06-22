"""会话导出器。

将 SessionRecord 导出为 Markdown 或 JSON 格式，便于分享与归档。
Markdown 包含元数据表格、工具调用汇总、完整对话历史。
"""

from __future__ import annotations

import json

from codepilot.session.storage import SessionRecord


class SessionExporter:
    """会话导出器，支持 Markdown 与 JSON 格式。"""

    def to_markdown(self, record: SessionRecord) -> str:
        """生成 Markdown 格式，包含元数据表格、工具调用汇总、对话历史。

        Args:
            record: 会话记录。

        Returns:
            Markdown 格式字符串。
        """
        lines: list[str] = []
        session_id = record.get("session_id", "")
        start_time = record.get("start_time", "")
        end_time = record.get("end_time") or "(进行中)"
        provider = record.get("provider", "")
        model = record.get("model", "")
        workspace = record.get("workspace_root", "")
        token_usage = record.get("token_usage", {})
        input_tokens = token_usage.get("input_tokens", 0)
        output_tokens = token_usage.get("output_tokens", 0)
        total_tokens = token_usage.get("total", 0)
        messages = record.get("messages", [])
        tool_calls = record.get("tool_calls", [])

        # 标题
        lines.append(f"# CodePilot 会话记录 {session_id}")
        lines.append("")

        # 元数据表格
        lines.append("## 元数据")
        lines.append("")
        lines.append("| 字段 | 值 |")
        lines.append("| --- | --- |")
        lines.append(f"| session_id | `{session_id}` |")
        lines.append(f"| 开始时间 | {start_time} |")
        lines.append(f"| 结束时间 | {end_time} |")
        lines.append(f"| provider | {provider} |")
        lines.append(f"| model | {model} |")
        lines.append(f"| workspace | {workspace} |")
        lines.append(f"| input_tokens | {input_tokens} |")
        lines.append(f"| output_tokens | {output_tokens} |")
        lines.append(f"| total_tokens | {total_tokens} |")
        lines.append("")

        # 工具调用汇总
        if tool_calls:
            lines.append("## 工具调用汇总")
            lines.append("")
            lines.append("| # | 工具 | 参数摘要 | 结果摘要 | 耗时(ms) | 时间戳 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for i, tc in enumerate(tool_calls, 1):
                name = tc.get("tool_name", "")
                duration = tc.get("duration_ms", 0)
                ts = tc.get("timestamp", "")
                # 参数摘要：截断到 200 字符
                args = tc.get("arguments", {})
                args_str = json.dumps(args, ensure_ascii=False) if args else ""
                if len(args_str) > 200:
                    args_str = args_str[:197] + "..."
                # 结果摘要：截断到 200 字符
                result_str = tc.get("result", "")
                if len(result_str) > 200:
                    result_str = result_str[:197] + "..."
                lines.append(
                    f"| {i} | {name} | {args_str} | {result_str} | {duration} | {ts} |"
                )
            lines.append("")

        # 对话历史
        lines.append("## 对话历史")
        lines.append("")
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            role_label = {
                "user": "🧑 User",
                "assistant": "🤖 Assistant",
                "tool": "🔧 Tool",
                "system": "⚙️ System",
            }.get(role, role)
            lines.append(f"### {role_label}")
            lines.append("")
            # assistant 消息的 thinking 折叠块
            if role == "assistant" and msg.get("thinking"):
                lines.append("<details><summary>🤔 Thinking</summary>")
                lines.append("")
                lines.append(msg["thinking"])
                lines.append("")
                lines.append("</details>")
                lines.append("")
            # assistant 消息为空但有 thinking 时，显示思考提示
            if role == "assistant" and not content and msg.get("thinking"):
                lines.append("[思考中...]")
            elif role == "tool":
                # tool 角色消息用代码块显示
                lines.append("```")
                lines.append(str(content))
                lines.append("```")
            else:
                lines.append(str(content))
            lines.append("")

        return "\n".join(lines)

    def to_json(self, record: SessionRecord) -> str:
        """导出 JSON 格式（带缩进，ensure_ascii=False）。

        Args:
            record: 会话记录。

        Returns:
            JSON 格式字符串。
        """
        return json.dumps(record, ensure_ascii=False, indent=2)


__all__ = ["SessionExporter"]
