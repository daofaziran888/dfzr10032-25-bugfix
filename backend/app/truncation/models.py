"""采集载荷截断明细的领域模型。

设计要点：
- 一份响应载荷（RawPayload）经 inspect_payload 校验后产出 PageInspection（截断明细）。
- 完整条目（EntryRecord）与断在字段中间的部分条目（PartialEntry）严格分开：
  部分条目不参与放行，只保留它在页内的位置，等待重采数据按位置关联。
- 接入通道（reconcile.py）消费这些明细，保证每个稳定键最终只交付一条。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PageStatus(str, Enum):
    """单页校验结论。

    TOTAL_DRIFT（声明总数漂移）不单独成状态：采集期间服务端会新增记录，
    总数对不上只记告警，完整页仍判 COMPLETE，不能据此把整页报错。
    """

    COMPLETE = "complete"          # 消息边界完整，页内条目均可交付
    TRUNCATED = "truncated"        # 响应在页尾被截断，存在丢失/断条
    MALFORMED = "malformed"        # 非截断类结构错误，无法恢复出可用前缀


class IssueCode(str, Enum):
    MISSING_END_BOUNDARY = "missing_end_boundary"     # 缺少消息结束边界
    MID_FIELD_BREAK = "mid_field_break"               # 末行断在字段中间
    DECLARED_COUNT_MISMATCH = "declared_count_mismatch"  # 声明条数与实收不一致
    TOTAL_DRIFT = "total_drift"                       # 服务端声明总数发生变化（仅告警）
    ENTRY_LOST = "entry_lost"                         # 页内条目丢失（序号不连续）
    KEY_MISSING = "key_missing"                       # 完整条目缺少稳定键
    KEY_DUPLICATED = "key_duplicated"                 # 页内稳定键重复（内容错位信号）
    ENTRY_MISALIGNED = "entry_misaligned"             # 同位置重采数据与原前缀对不上
    OUT_OF_PAGE_ORDER = "out_of_page_order"           # 重采条目越过待补采位置


@dataclass
class EntryRecord:
    """一条已确认完整、可参与放行/对账的事件。"""

    page_no: int
    position: int                     # 页内位置，从 1 开始
    key: str | None                   # 稳定键（运单号），缺失时为 None
    data: dict[str, Any]


@dataclass
class PartialEntry:
    """断在字段中间的末条：不可交付，只保留页内位置与已收到的残片。"""

    page_no: int
    position: int                     # 这条在页内应该占据的位置
    raw_fragment: str                 # 已收到的残片原文（不含外层数组逗号）
    char_offset: int                  # 残片起点在原始载荷中的字符偏移
    fragment_preview: str             # 截断现场的可读预览
    expected_key_hint: str | None = None  # 残片中能辨认出的键（如只解析出一半字段）


@dataclass
class PageIssue:
    """单页上的一条校验发现。"""

    code: IssueCode
    message: str
    position: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageInspection:
    """一份响应载荷的截断明细（采集载荷截断明细的一页）。"""

    page_no: int
    status: PageStatus
    declared_count: int | None        # 响应声明的本页条数（events 数组长度声明）
    received_complete: int            # 实际收到且结构完整的条目数
    declared_total: int | None        # 服务端声明的全量总数
    end_boundary_present: bool        # 消息结束边界是否到位
    complete_entries: list[EntryRecord] = field(default_factory=list)
    partial_entry: PartialEntry | None = None
    issues: list[PageIssue] = field(default_factory=list)
    raw_length: int = 0

    @property
    def is_truncated(self) -> bool:
        return self.status == PageStatus.TRUNCATED

    @property
    def missing_positions(self) -> list[int]:
        """声明条数内、既无完整条目也无断条占位的页内位置（条目丢失定位）。"""
        if self.declared_count is None:
            return []
        occupied = {entry.position for entry in self.complete_entries}
        if self.partial_entry is not None:
            occupied.add(self.partial_entry.position)
        return [pos for pos in range(1, self.declared_count + 1) if pos not in occupied]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_no": self.page_no,
            "status": self.status.value,
            "declared_count": self.declared_count,
            "received_complete": self.received_complete,
            "declared_total": self.declared_total,
            "end_boundary_present": self.end_boundary_present,
            "complete_entries": [
                {"page_no": e.page_no, "position": e.position, "key": e.key, "data": e.data}
                for e in self.complete_entries
            ],
            "partial_entry": None
            if self.partial_entry is None
            else {
                "page_no": self.partial_entry.page_no,
                "position": self.partial_entry.position,
                "raw_fragment": self.partial_entry.raw_fragment,
                "char_offset": self.partial_entry.char_offset,
                "fragment_preview": self.partial_entry.fragment_preview,
                "expected_key_hint": self.partial_entry.expected_key_hint,
            },
            "missing_positions": self.missing_positions,
            "issues": [
                {"code": i.code.value, "message": i.message, "position": i.position, "detail": i.detail}
                for i in self.issues
            ],
            "raw_length": self.raw_length,
        }


@dataclass
class RawPayload:
    """一次响应收到的原始载荷。

    raw_text 是响应体原文（可能在任意字符处被截断）；page_no/declared 来自请求登记，
    用于和响应内的声明交叉校验。
    """

    page_no: int
    raw_text: str
    attempt: int = 1                  # 第几次采集（首次为 1，重试依次递增）
    request_declared_count: int | None = None
    request_declared_total: int | None = None
