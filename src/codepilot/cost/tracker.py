"""Token 费用追踪器。

每次 LLM 调用后累计 token 用量，/cost 命令显示详细费用估算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 价格表：$/1M tokens
PRICE_TABLE: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "astron-code-latest": {"input": 0.50, "output": 2.00},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}


@dataclass
class UsageRecord:
    """单次 LLM 调用的用量记录。"""
    model: str
    input_tokens: int
    output_tokens: int
    timestamp: float = 0.0


@dataclass
class CostTracker:
    """会话级费用追踪器。"""

    records: list[UsageRecord] = field(default_factory=list)

    def record_usage(self, model: str, input_tokens: int, output_tokens: int, timestamp: float = 0.0) -> None:
        """记录一次 LLM 调用的 token 用量。"""
        self.records.append(UsageRecord(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=timestamp,
        ))
        logger.debug("费用记录", model=model, input=input_tokens, output=output_tokens)

    def get_session_totals(self) -> dict[str, Any]:
        """获取本次会话的累计统计。"""
        total_input = sum(r.input_tokens for r in self.records)
        total_output = sum(r.output_tokens for r in self.records)
        total_cost = self._estimate_cost(total_input, total_output)
        by_model: dict[str, dict[str, int]] = {}
        for r in self.records:
            if r.model not in by_model:
                by_model[r.model] = {"input": 0, "output": 0}
            by_model[r.model]["input"] += r.input_tokens
            by_model[r.model]["output"] += r.output_tokens
        return {
            "total_input": total_input,
            "total_output": total_output,
            "total_cost": total_cost,
            "by_model": by_model,
            "num_calls": len(self.records),
        }

    def get_last_usage(self) -> UsageRecord | None:
        """获取最近一次调用记录。"""
        return self.records[-1] if self.records else None

    def _estimate_cost(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        """估算费用（美元）。"""
        model = model or (self.records[-1].model if self.records else "")
        prices = PRICE_TABLE.get(model, {"input": 0.50, "output": 2.00})  # 默认价格
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        return cost

    def format_report(self) -> str:
        """格式化费用报告。"""
        totals = self.get_session_totals()
        last = self.get_last_usage()
        lines = ["Session Cost Estimate", "=" * 40]
        if last:
            last_cost = self._estimate_cost(last.input_tokens, last.output_tokens, last.model)
            lines.append(f"Last call:  {last.model}")
            lines.append(f"  Input: {last.input_tokens:,} | Output: {last.output_tokens:,} | Est: ${last_cost:.4f}")
        lines.append(f"\nSession total:")
        lines.append(f"  Input: {totals['total_input']:,} | Output: {totals['total_output']:,} | Est: ${totals['total_cost']:.4f}")
        lines.append(f"  Total calls: {totals['num_calls']}")
        if totals['by_model']:
            lines.append(f"\nBy model:")
            for model, usage in totals['by_model'].items():
                cost = self._estimate_cost(usage['input'], usage['output'], model)
                lines.append(f"  {model}: {usage['input']:,} in + {usage['output']:,} out = ${cost:.4f}")
        lines.append("\nPrices are estimates. Check provider dashboard for actual charges.")
        return "\n".join(lines)
