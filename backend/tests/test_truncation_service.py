"""端到端场景测试：通过 TruncationService（路由层直接调用的服务对象）走完整链路。

覆盖需求里的完整业务时序：
首次截断（37/50 断开）→ 重试前放行查询 → 重试（总数 50→52、前 20 条重发）→ 整页对账。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.truncation import TruncationService  # noqa: E402

KEY = "waybill_no"


def event(seq: int, *, total: int = 50) -> dict:
    return {
        "waybill_no": f"WB{seq:04d}",
        "seq": seq,
        "origin": "上海仓",
        "destination": "北京站",
        "total_snapshot": total,
    }


def truncated_response(events: list[dict], fragment: str, *, total: int, count: int) -> str:
    head = json.dumps({"code": 0, "total": total, "declared_count": count, "events": events},
                      ensure_ascii=False)
    return head[: head.rfind("]")] + ", " + fragment


def full_response(events: list[dict], *, total: int, count: int) -> str:
    return json.dumps({"code": 0, "total": total, "declared_count": count, "events": events},
                      ensure_ascii=False)


class TruncationServiceEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.svc = TruncationService()
        self.channel = "SHIP-CH-01"

    def test_full_lifecycle(self):
        # 1) 首次采集：50 条声明，第 37 条字段中间断开
        fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京'
        first = truncated_response(
            [event(i) for i in range(1, 37)], fragment, total=50, count=50
        )
        detail = self.svc.register_payload(
            channel_code=self.channel, page_no=1, raw_text=first,
            attempt=1, request_declared_count=50, request_declared_total=50,
        )
        self.assertEqual(detail["status"], "truncated")
        self.assertEqual(detail["received_complete"], 36)
        self.assertEqual(detail["partial_entry"]["position"], 37)
        self.assertEqual(detail["partial_entry"]["expected_key_hint"], "WB0037")
        self.assertEqual(detail["missing_positions"], list(range(38, 51)))

        # 2) 重试回来前：装运侧查可放行运单与等待页尾的位置
        view = self.svc.releasable(self.channel, page_no=1)
        self.assertTrue(view["found"])
        self.assertEqual(len(view["releasable"]), 36)
        self.assertEqual(view["releasable"][0]["key"], "WB0001")
        self.assertEqual(view["releasable"][-1]["key"], "WB0036")
        pending = view["pending_pages"][0]
        self.assertEqual(pending["partial_position"], 37)
        self.assertEqual(pending["missing_positions"], list(range(38, 51)))
        self.assertFalse(pending["page_complete"])

        # 3) 第一次重试：总数更正 52，前 20 条重发，补到第 40 条
        retry1 = full_response(
            [{**event(i, total=52), "_position": i} for i in range(1, 21)]
            + [{**event(i, total=52), "_position": i} for i in range(37, 41)],
            total=52, count=52,
        )
        outcome1 = self.svc.register_retry(
            channel_code=self.channel, page_no=1, raw_text=retry1,
            attempt=2, request_declared_count=50, request_declared_total=50,
        )
        self.assertEqual(len(outcome1["duplicates_skipped"]), 20)
        self.assertEqual(outcome1["newly_delivered"], [f"WB{i:04d}" for i in range(37, 41)])
        self.assertTrue(any(i["code"] == "total_drift" for i in outcome1["issues"]))
        self.assertFalse(outcome1["page_complete"])

        # 每键只占一条：40 个可放行键，无重复
        view2 = self.svc.releasable(self.channel, page_no=1)
        keys = [r["key"] for r in view2["releasable"]]
        self.assertEqual(len(keys), 40)
        self.assertEqual(len(set(keys)), 40)

        # 4) 第二次重试：补齐 41..52，整页核齐
        retry2 = full_response(
            [{**event(i, total=52), "_position": i} for i in range(1, 53)],
            total=52, count=52,
        )
        outcome2 = self.svc.register_retry(
            channel_code=self.channel, page_no=1, raw_text=retry2,
            attempt=3, request_declared_count=50, request_declared_total=50,
        )
        self.assertTrue(outcome2["page_complete"])
        self.assertEqual(outcome2["newly_delivered"], [f"WB{i:04d}" for i in range(41, 53)])
        self.assertEqual(len(outcome2["duplicates_skipped"]), 40)

        # 5) 结算对账：按原页序、键唯一、无缺口、每键一条
        report = self.svc.reconcile(self.channel, 1)
        self.assertTrue(report["page_complete"])
        self.assertEqual(report["delivered_count"], 52)
        self.assertEqual(report["unique_key_count"], 52)
        self.assertEqual(report["gaps"], [])
        self.assertEqual([e["position"] for e in report["ordered_entries"]], list(range(1, 53)))
        self.assertEqual([e["key"] for e in report["ordered_entries"]],
                         [f"WB{i:04d}" for i in range(1, 53)])
        # 最新总数保留为 52（漂移后的值），但没有把完整页判错
        self.assertEqual(report["declared_total_latest"], 52)

        # 6) 通道汇总
        ov = self.svc.overview(self.channel)
        self.assertEqual(ov["total_delivered"], 52)
        self.assertEqual(ov["pages"][0]["page_complete"], True)

    def test_unknown_channel_returns_not_found_view(self):
        view = self.svc.releasable("NOPE")
        self.assertFalse(view["found"])
        self.assertEqual(view["releasable"], [])
        self.assertFalse(self.svc.overview("NOPE")["found"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
