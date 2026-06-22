"""CostTracker 单元测试。

覆盖：record_usage、get_session_totals、format_report、空追踪器、多模型。
"""

from __future__ import annotations

from codepilot.cost.tracker import CostTracker, UsageRecord


def test_record_usage():
    """record_usage 应正确追加记录。"""
    tracker = CostTracker()
    tracker.record_usage("deepseek-chat", 100, 50)
    tracker.record_usage("deepseek-chat", 200, 100, timestamp=1.0)

    assert len(tracker.records) == 2
    assert tracker.records[0].model == "deepseek-chat"
    assert tracker.records[0].input_tokens == 100
    assert tracker.records[0].output_tokens == 50
    assert tracker.records[1].input_tokens == 200
    assert tracker.records[1].output_tokens == 100
    assert tracker.records[1].timestamp == 1.0


def test_get_session_totals():
    """get_session_totals 应返回正确的累计统计。"""
    tracker = CostTracker()
    tracker.record_usage("deepseek-chat", 1000, 500)
    tracker.record_usage("deepseek-chat", 2000, 1000)

    totals = tracker.get_session_totals()
    assert totals["total_input"] == 3000
    assert totals["total_output"] == 1500
    assert totals["num_calls"] == 2
    assert "deepseek-chat" in totals["by_model"]
    assert totals["by_model"]["deepseek-chat"]["input"] == 3000
    assert totals["by_model"]["deepseek-chat"]["output"] == 1500
    # deepseek-chat: input=0.27/1M, output=1.10/1M
    # cost = (3000 * 0.27 + 1500 * 1.10) / 1_000_000
    expected_cost = (3000 * 0.27 + 1500 * 1.10) / 1_000_000
    assert abs(totals["total_cost"] - expected_cost) < 1e-10


def test_format_report():
    """format_report 应包含关键信息。"""
    tracker = CostTracker()
    tracker.record_usage("deepseek-chat", 1000, 500)

    report = tracker.format_report()
    assert "Session Cost Estimate" in report
    assert "deepseek-chat" in report
    assert "1,000" in report
    assert "500" in report
    assert "$" in report
    assert "Total calls: 1" in report
    assert "Prices are estimates" in report


def test_empty_tracker():
    """空追踪器应返回零值统计。"""
    tracker = CostTracker()

    totals = tracker.get_session_totals()
    assert totals["total_input"] == 0
    assert totals["total_output"] == 0
    assert totals["total_cost"] == 0.0
    assert totals["num_calls"] == 0
    assert totals["by_model"] == {}

    assert tracker.get_last_usage() is None

    report = tracker.format_report()
    assert "Session Cost Estimate" in report
    assert "Total calls: 0" in report


def test_multiple_models():
    """多模型追踪应正确按模型分组。"""
    tracker = CostTracker()
    tracker.record_usage("deepseek-chat", 1000, 500)
    tracker.record_usage("claude-sonnet-4-20250514", 2000, 1000)
    tracker.record_usage("deepseek-chat", 500, 200)

    totals = tracker.get_session_totals()
    assert totals["total_input"] == 3500
    assert totals["total_output"] == 1700
    assert totals["num_calls"] == 3
    assert len(totals["by_model"]) == 2
    assert totals["by_model"]["deepseek-chat"]["input"] == 1500
    assert totals["by_model"]["deepseek-chat"]["output"] == 700
    assert totals["by_model"]["claude-sonnet-4-20250514"]["input"] == 2000
    assert totals["by_model"]["claude-sonnet-4-20250514"]["output"] == 1000

    # 最后一次调用是 deepseek-chat
    last = tracker.get_last_usage()
    assert last is not None
    assert last.model == "deepseek-chat"
    assert last.input_tokens == 500

    # format_report 应包含两个模型
    report = tracker.format_report()
    assert "deepseek-chat" in report
    assert "claude-sonnet-4-20250514" in report
    assert "By model:" in report
