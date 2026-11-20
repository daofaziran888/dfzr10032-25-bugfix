"""采集载荷截断明细与接入对账的单元测试（仅标准库，python3 -m unittest 即可跑）。

场景严格对应需求：
- 接口声明 50 条装运事件，第 37 条在字段中间断开，前 36 条带稳定运单键；
- 重试时服务端总数更正为 52，且前 20 条被重新送来（不得重复交付）；
- 重试回来前要能查到哪些运单可放行、哪些位置在等页尾；
- 每个已放行键在后续对账中只占一条；
- 另含总数漂移不误判、断条位置保留、内容错位拦截三组边界用例。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.truncation import (  # noqa: E402
    IssueCode,
    IngestionChannel,
    PageStatus,
    RawPayload,
    inspect_payload,
)

KEY = "waybill_no"


def event(seq: int, *, total: int = 50) -> dict:
    return {
        "waybill_no": f"WB{seq:04d}",
        "seq": seq,
        "origin": "上海仓",
        "destination": "北京站",
        "declared_total_snapshot": total,
    }


def build_envelope(events: list[dict], *, total: int, declared_count: int,
                   closed: bool = True, partial_fragment: str | None = None) -> str:
    """构造响应原文；closed=False 且给 partial_fragment 时模拟字段中间截断。"""
    head = json.dumps({"code": 0, "total": total, "declared_count": declared_count,
                       "events": events}, ensure_ascii=False)
    if closed:
        return head
    # 在 events 数组里完整对象之后接上断条残片，整体不闭合
    prefix = head[: head.rfind("]")]
    if partial_fragment:
        prefix = prefix + ", " + partial_fragment
    return prefix


class TruncationInspectorTests(unittest.TestCase):
    def test_mid_field_break_recovers_full_prefix_and_position(self):
        # 前 36 条完整，第 37 条断在字段中间（destination 的字符串只写了一半）
        events = [event(i) for i in range(1, 37)]
        fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京'
        raw = build_envelope(events, total=50, declared_count=50,
                             closed=False, partial_fragment=fragment)

        inspection = inspect_payload(
            RawPayload(page_no=1, raw_text=raw, attempt=1,
                       request_declared_count=50, request_declared_total=50),
            key_field=KEY,
        )

        self.assertEqual(inspection.status, PageStatus.TRUNCATED)
        self.assertFalse(inspection.end_boundary_present)
        self.assertEqual(inspection.received_complete, 36)
        self.assertEqual(inspection.declared_count, 50)
        # 断条保留页内位置 37 与字符偏移，且能从残片辨认出运单键
        self.assertIsNotNone(inspection.partial_entry)
        self.assertEqual(inspection.partial_entry.position, 37)
        self.assertEqual(inspection.partial_entry.expected_key_hint, "WB0037")
        self.assertGreater(inspection.partial_entry.char_offset, 0)
        # 38..50 是条目丢失位置
        self.assertEqual(inspection.missing_positions, list(range(38, 51)))
        codes = {i.code for i in inspection.issues}
        self.assertIn(IssueCode.MID_FIELD_BREAK, codes)
        self.assertIn(IssueCode.ENTRY_LOST, codes)
        self.assertIn(IssueCode.DECLARED_COUNT_MISMATCH, codes)
        # 前 36 条稳定键齐全
        self.assertEqual([e.key for e in inspection.complete_entries],
                         [f"WB{i:04d}" for i in range(1, 37)])

    def test_total_drift_does_not_fail_a_complete_page(self):
        # 完整闭合的一页，只是服务端总数从 50 变成 52（采集期间新增了记录）
        events = [event(i, total=52) for i in range(1, 31)]
        raw = build_envelope(events, total=52, declared_count=30)
        inspection = inspect_payload(
            RawPayload(page_no=2, raw_text=raw, request_declared_count=30,
                       request_declared_total=50),
            key_field=KEY,
        )
        self.assertEqual(inspection.status, PageStatus.COMPLETE)
        self.assertTrue(inspection.end_boundary_present)
        codes = {i.code for i in inspection.issues}
        self.assertIn(IssueCode.TOTAL_DRIFT, codes)
        self.assertNotIn(IssueCode.DECLARED_COUNT_MISMATCH, codes)

    def test_clean_boundary_truncation_has_no_partial_entry(self):
        # 截在完整元素边界上（逗号之后即断），没有字段中间断条
        events = [event(i) for i in range(1, 11)]
        raw = build_envelope(events, total=50, declared_count=50, closed=False)
        inspection = inspect_payload(
            RawPayload(page_no=1, raw_text=raw, request_declared_count=50),
            key_field=KEY,
        )
        self.assertEqual(inspection.status, PageStatus.TRUNCATED)
        self.assertIsNone(inspection.partial_entry)
        self.assertIn(IssueCode.MISSING_END_BOUNDARY, {i.code for i in inspection.issues})
        self.assertEqual(inspection.received_complete, 10)

    def test_duplicate_key_inside_page_flagged(self):
        events = [event(1), event(2)]
        events[1]["waybill_no"] = "WB0001"  # 同页键重复 -> 内容错位信号
        raw = build_envelope(events, total=2, declared_count=2)
        inspection = inspect_payload(RawPayload(page_no=1, raw_text=raw), key_field=KEY)
        self.assertIn(IssueCode.KEY_DUPLICATED, {i.code for i in inspection.issues})


class IngestionChannelScenarioTests(unittest.TestCase):
    def setUp(self):
        self.channel = IngestionChannel(key_field=KEY)
        # 首次响应：50 条声明，第 37 条字段中间断开
        fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京'
        self.first_raw = build_envelope(
            [event(i) for i in range(1, 37)], total=50, declared_count=50,
            closed=False, partial_fragment=fragment,
        )

    def test_query_before_retry_returns_36_releasable_and_pending_tail(self):
        inspection = self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )
        self.assertEqual(inspection.status, PageStatus.TRUNCATED)

        releasable = self.channel.releasable_waybills(1)
        self.assertEqual(len(releasable), 36)
        self.assertEqual([r["key"] for r in releasable],
                         [f"WB{i:04d}" for i in range(1, 37)])
        self.assertTrue(all(r["status"] == "可放行" for r in releasable))

        pending = self.channel.pending_positions(1)
        self.assertEqual(pending["partial_position"], 37)
        self.assertEqual(pending["partial_key_hint"], "WB0037")
        self.assertEqual(pending["missing_positions"], list(range(38, 51)))
        self.assertFalse(pending["page_complete"])

    def test_retry_dedup_prefix_and_fill_tail_without_double_delivery(self):
        self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )

        # 第一次重试：总数更正为 52；前 20 条被重新送来，并补采到第 40 条后再次截断
        # （重发前缀带原页内位置 1..20，新补条目带位置 37..40，按原页序关联）
        retry1_events = [
            {**event(i, total=52), "_position": i} for i in range(1, 21)
        ] + [
            {**event(i, total=52), "_position": i} for i in range(37, 41)
        ]
        retry1_raw = build_envelope(retry1_events, total=52, declared_count=52)
        outcome1 = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=retry1_raw, attempt=2,
                       request_declared_count=50, request_declared_total=50)
        )
        # 前 20 条重发前缀全部识别为重复，不二次交付
        self.assertEqual(outcome1.duplicates_skipped,
                         [f"WB{i:04d}" for i in range(1, 21)])
        # 37..40 是按待补采位置新交付的
        self.assertEqual(outcome1.newly_delivered,
                         [f"WB{i:04d}" for i in range(37, 41)])
        # 漂移被带到重试结果里，但不阻断
        self.assertIn(IssueCode.TOTAL_DRIFT, {i.code for i in outcome1.issues})
        # 通道里每个键仍只有一条
        self.assertEqual(len(self.channel.releasable_waybills(1)), 40)
        self.assertEqual(
            sorted(self.channel.overview()["releasable_keys"]),
            [f"WB{i:04d}" for i in range(1, 41)],
        )
        pending = self.channel.pending_positions(1)
        self.assertEqual(pending["missing_positions"], list(range(41, 53)))

        # 第二次重试：补齐 41..52，整页核齐（1..40 随响应再次重发）
        retry2_events = [
            {**event(i, total=52), "_position": i} for i in range(1, 53)
        ]
        retry2_raw = build_envelope(retry2_events, total=52, declared_count=52)
        outcome2 = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=retry2_raw, attempt=3,
                       request_declared_count=50, request_declared_total=50)
        )
        self.assertEqual(outcome2.newly_delivered,
                         [f"WB{i:04d}" for i in range(41, 53)])
        # 1..40 全部识别为重复，且没有任何二次交付
        self.assertEqual(len(outcome2.duplicates_skipped), 40)
        self.assertTrue(outcome2.page_complete)

        # 结算对账：按原页序 1..52，键唯一且每键一条
        report = self.channel.reconcile_page(1)
        self.assertTrue(report["page_complete"])
        self.assertEqual(report["delivered_count"], 52)
        self.assertEqual(report["unique_key_count"], 52)
        self.assertEqual([e["position"] for e in report["ordered_entries"]],
                         list(range(1, 53)))
        self.assertEqual([e["key"] for e in report["ordered_entries"]],
                         [f"WB{i:04d}" for i in range(1, 53)])
        self.assertEqual(report["gaps"], [])

    def test_mid_field_partial_kept_until_matching_retry_fills_same_position(self):
        self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )
        # 重试响应在 37 位置仍是断条：位置 37 继续挂起
        fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京朝'
        raw = build_envelope([event(i) for i in range(1, 37)], total=50,
                             declared_count=50, closed=False, partial_fragment=fragment)
        outcome = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=raw, attempt=2,
                       request_declared_count=50, request_declared_total=50)
        )
        self.assertEqual(outcome.newly_delivered, [])
        self.assertEqual(self.channel.pending_positions(1)["partial_position"], 37)
        self.assertFalse(outcome.page_complete)

    def test_misaligned_retry_at_partial_position_is_flagged_not_silent(self):
        self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )
        # 构造重试：在位置 37 送来一个不同运单键，模拟内容错位
        events = [event(i) for i in range(1, 37)]
        wrong = event(37)
        wrong["waybill_no"] = "WB9999"
        events.append(wrong)
        raw = build_envelope(events, total=50, declared_count=50)
        outcome = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=raw, attempt=2,
                       request_declared_count=50, request_declared_total=50)
        )
        # 37 位置原是断条挂起（未交付），重采值作为权威新值交付，但必须显式标记错位
        self.assertIn("WB9999", outcome.newly_delivered)
        misalign = [i for i in outcome.issues if i.code == IssueCode.ENTRY_MISALIGNED]
        self.assertTrue(misalign)
        self.assertEqual(misalign[0].position, 37)
        self.assertEqual(misalign[0].detail["expected_hint"], "WB0037")
        report = self.channel.reconcile_page(1)
        self.assertEqual(report["ordered_entries"][36]["key"], "WB9999")

    def test_out_of_order_retry_entry_is_refused(self):
        self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )
        # 位置 5 已有 WB0005；重采用一个新键占用位置 5，属于越过待补采位置
        events = [event(i) for i in range(1, 37)]
        events[4] = dict(events[4])
        events[4]["waybill_no"] = "WB7777"
        raw = build_envelope(events, total=50, declared_count=50)
        outcome = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=raw, attempt=2,
                       request_declared_count=50, request_declared_total=50)
        )
        self.assertIn(IssueCode.OUT_OF_PAGE_ORDER, {i.code for i in outcome.issues})
        self.assertNotIn("WB7777", self.channel.overview()["releasable_keys"])
        # 原前缀保持不动
        self.assertEqual(
            self.channel.reconcile_page(1)["ordered_entries"][4]["key"], "WB0005"
        )


    def test_shrunk_retry_declared_count_does_not_shrink_pending_tail(self):
        """重试只回重发前缀+少量补采（本页条数声明变小）时，页尾补采不得被误判越界。"""
        self.channel.ingest(
            RawPayload(page_no=1, raw_text=self.first_raw, attempt=1,
                       request_declared_count=50, request_declared_total=50)
        )
        # 响应只含 24 个事件：前 20 条重发 + 37..40 补采，本页声明 24，但整页仍是 50
        retry_events = [
            {**event(i), "_position": i} for i in range(1, 21)
        ] + [
            {**event(i), "_position": i} for i in range(37, 41)
        ]
        head = json.dumps({"code": 0, "total": 50, "declared_count": 24,
                           "events": retry_events}, ensure_ascii=False)
        outcome = self.channel.apply_retry(
            RawPayload(page_no=1, raw_text=head, attempt=2,
                       request_declared_count=50, request_declared_total=50)
        )
        self.assertEqual(outcome.newly_delivered,
                         [f"WB{i:04d}" for i in range(37, 41)])
        self.assertNotIn(IssueCode.OUT_OF_PAGE_ORDER, {i.code for i in outcome.issues})
        # 整页声明仍保持 50，待补采位置是 41..50
        self.assertEqual(self.channel.pending_positions(1)["missing_positions"],
                         list(range(41, 51)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
