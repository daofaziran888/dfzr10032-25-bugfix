"""题设场景的可运行演示：python3 scripts/demo_truncation.py

时序：
1. 接口声明 50 条装运事件，第 37 条在字段中间断开，前 36 条带稳定运单键；
2. 重试回来前，查询哪些运单已可放行、哪些位置仍在等页尾；
3. 第一次重试：服务端总数更正为 52，前 20 条被重新送来（去重，不重复交付），补到第 40 条；
4. 第二次重试：补齐 41..52，整页按原页序与键值核齐。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.truncation import TruncationService  # noqa: E402

CHANNEL = "SHIP-CH-01"


def event(seq: int, total: int) -> dict:
    return {
        "waybill_no": f"WB{seq:04d}",
        "seq": seq,
        "origin": "上海仓",
        "destination": "北京站",
        "total_snapshot": total,
    }


def envelope(events: list[dict], total: int, count: int) -> str:
    return json.dumps({"code": 0, "total": total, "declared_count": count, "events": events},
                      ensure_ascii=False)


def truncated(events: list[dict], fragment: str, total: int, count: int) -> str:
    head = envelope(events, total, count)
    return head[: head.rfind("]")] + ", " + fragment


def main() -> None:
    svc = TruncationService()

    print("== 第 1 步：首次采集响应（50 条声明，第 37 条字段中间断开） ==")
    fragment = '{"waybill_no": "WB0037", "seq": 37, "origin": "上海仓", "destination": "北京'
    detail = svc.register_payload(
        channel_code=CHANNEL, page_no=1,
        raw_text=truncated([event(i, 50) for i in range(1, 37)], fragment, 50, 50),
        attempt=1, request_declared_count=50, request_declared_total=50,
    )
    print(f"页状态={detail['status']}，完整收到={detail['received_complete']} 条，"
          f"结束边界={'有' if detail['end_boundary_present'] else '缺失'}")
    print(f"断条：第 {detail['partial_entry']['position']} 条，"
          f"残片键提示={detail['partial_entry']['expected_key_hint']}，"
          f"字符偏移={detail['partial_entry']['char_offset']}")
    print(f"条目丢失位置：{detail['missing_positions']}")

    print("\n== 第 2 步：重试回来前的放行查询 ==")
    view = svc.releasable(CHANNEL, page_no=1)
    keys = [r["key"] for r in view["releasable"]]
    print(f"可放行运单 {len(keys)} 个：{keys[0]} .. {keys[-1]}（每键一条）")
    pending = view["pending_pages"][0]
    print(f"等待页尾：断条位置={pending['partial_position']}，"
          f"丢失位置={pending['missing_positions']}，整页核齐={pending['page_complete']}")

    print("\n== 第 3 步：第一次重试（总数 50->52，前 20 条重发，补到第 40 条） ==")
    retry1 = [
        *[{**event(i, 52), "_position": i} for i in range(1, 21)],
        *[{**event(i, 52), "_position": i} for i in range(37, 41)],
    ]
    outcome1 = svc.register_retry(
        channel_code=CHANNEL, page_no=1, raw_text=envelope(retry1, 52, len(retry1)),
        attempt=2, request_declared_count=50, request_declared_total=50,
    )
    print(f"去重跳过 {len(outcome1['duplicates_skipped'])} 条重发前缀，"
          f"新交付 {outcome1['newly_delivered']}")
    print(f"告警：{[i['code'] for i in outcome1['issues']]}")
    view2 = svc.releasable(CHANNEL, page_no=1)
    now_keys = [r["key"] for r in view2["releasable"]]
    print(f"当前可放行 {len(now_keys)} 个，去重后仍唯一={len(now_keys) == len(set(now_keys))}")
    print(f"仍待补采位置：{view2['pending_pages'][0]['missing_positions']}")

    print("\n== 第 4 步：第二次重试（补齐 41..52） ==")
    retry2 = [{**event(i, 52), "_position": i} for i in range(1, 53)]
    outcome2 = svc.register_retry(
        channel_code=CHANNEL, page_no=1, raw_text=envelope(retry2, 52, 52),
        attempt=3, request_declared_count=50, request_declared_total=50,
    )
    print(f"新交付 {len(outcome2['newly_delivered'])} 条，"
          f"去重跳过 {len(outcome2['duplicates_skipped'])} 条，整页核齐={outcome2['page_complete']}")

    print("\n== 第 5 步：结算整页对账（按原页序 + 键值） ==")
    report = svc.reconcile(CHANNEL, 1)
    print(f"交付={report['delivered_count']}，唯一键={report['unique_key_count']}，"
          f"缺口={report['gaps']}，最新总数={report['declared_total_latest']}")
    print("前 3 条：", [(e["position"], e["key"]) for e in report["ordered_entries"][:3]])
    print("末 3 条：", [(e["position"], e["key"]) for e in report["ordered_entries"][-3:]])


if __name__ == "__main__":
    main()
