"""数据接入通道：消费采集载荷截断明细，做重采关联与对账。

口径（对应业务约束）：
- 装运人员按“已接入的完整事件”放行单票：只有结构完整、带稳定键的条目进入可放行集合；
  断在字段中间的条目在重采补齐前不可放行。
- 每个稳定键全局只交付一次：重试时重新送来的前缀（如前 20 条）识别为重复，
  记一笔对账记录但绝不二次交付。
- 截断页的完整前缀与重采数据按“原页序 + 位置”实际关联：重采条目必须落在待补采
  位置上，越界或同位置键对不上都判内容错位，而不是静默覆盖。
- 结算人员按原页序核齐整页：整页只有在无断条、无丢失位置时才算核齐（page_complete），
  总数漂移不影响整页判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.truncation.inspector import inspect_payload
from app.truncation.models import (
    EntryRecord,
    IssueCode,
    PageInspection,
    PageIssue,
    PageStatus,
    RawPayload,
)


@dataclass
class RetryOutcome:
    """一次重试响应与既有截断前缀的关联结果。"""

    attempt: int
    page_no: int
    newly_delivered: list[str] = field(default_factory=list)   # 本次新交付的稳定键
    duplicates_skipped: list[str] = field(default_factory=list)  # 已交付过、被去重跳过的键
    still_missing_positions: list[int] = field(default_factory=list)
    issues: list[PageIssue] = field(default_factory=list)
    page_complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "page_no": self.page_no,
            "newly_delivered": self.newly_delivered,
            "duplicates_skipped": self.duplicates_skipped,
            "still_missing_positions": self.still_missing_positions,
            "page_complete": self.page_complete,
            "issues": [
                {"code": i.code.value, "message": i.message, "position": i.position, "detail": i.detail}
                for i in self.issues
            ],
        }


@dataclass
class _PageState:
    """单页在通道内的累积状态。"""

    page_no: int
    declared_count: int | None
    # 位置 -> 已接入条目（完整前缀 + 重采补齐，按原页序归位）
    by_position: dict[int, EntryRecord] = field(default_factory=dict)
    delivered_keys: set[str] = field(default_factory=set)
    partial_position: int | None = None
    partial_key_hint: str | None = None
    page_complete: bool = False
    last_declared_total: int | None = None
    history: list[PageInspection] = field(default_factory=list)
    retry_outcomes: list[RetryOutcome] = field(default_factory=list)

    def missing_positions(self) -> list[int]:
        """丢失位置：声明范围内既没有已接入条目、也不是断条挂起点的位置。

        断条位置由 partial_position 单独表达，不能与丢失位置混在一起。
        """
        if self.declared_count is None:
            return []
        occupied = set(self.by_position)
        if self.partial_position is not None:
            occupied.add(self.partial_position)
        return [pos for pos in range(1, self.declared_count + 1) if pos not in occupied]


class IngestionChannel:
    """按页维护接入状态，供装运放行查询与结算整页对账。"""

    def __init__(self, *, key_field: str = "waybill_no") -> None:
        self.key_field = key_field
        self._pages: dict[int, _PageState] = {}
        # 稳定键 -> (页号, 位置)，保证每个键在整个通道只占一条
        self._key_index: dict[str, tuple[int, int]] = {}

    def page_numbers(self) -> list[int]:
        """通道内已有采集记录的页号，按原页序返回。"""
        return sorted(self._pages)

    # ------------------------------------------------------------------ #
    # 首次采集
    # ------------------------------------------------------------------ #
    def ingest(self, payload: RawPayload) -> PageInspection:
        """登记并校验一份首次采集响应，完整前缀直接接入、断条挂起等待。"""
        inspection = inspect_payload(payload, key_field=self.key_field)
        state = self._pages.get(inspection.page_no)
        if state is None:
            state = _PageState(
                page_no=inspection.page_no,
                declared_count=inspection.declared_count,
            )
            self._pages[inspection.page_no] = state
        state.history.append(inspection)
        state.last_declared_total = inspection.declared_total
        if inspection.declared_count is not None:
            state.declared_count = inspection.declared_count

        for entry in inspection.complete_entries:
            self._adopt_entry(state, entry, inspection)

        if inspection.partial_entry is not None:
            state.partial_position = inspection.partial_entry.position
            state.partial_key_hint = inspection.partial_entry.expected_key_hint
        elif inspection.status == PageStatus.COMPLETE:
            state.partial_position = None
            state.page_complete = not state.missing_positions()

        return inspection

    def _adopt_entry(self, state: _PageState, entry: EntryRecord, inspection: PageInspection) -> None:
        """把一条完整条目接入通道；无键或同键重复只登记问题，不产生交付。"""
        if entry.key is None:
            return  # 缺键问题已在 inspector 记录，通道不予放行
        if entry.key in self._key_index:
            owner_page, owner_pos = self._key_index[entry.key]
            if (owner_page, owner_pos) != (state.page_no, entry.position):
                inspection.issues.append(PageIssue(
                    IssueCode.KEY_DUPLICATED,
                    f"稳定键 {entry.key} 已在第 {owner_page} 页第 {owner_pos} 条交付，跳过重复交付",
                    position=entry.position,
                    detail={"key": entry.key, "owner_page": owner_page, "owner_position": owner_pos},
                ))
            return
        # 同位置若已有条目（重采覆盖场景），键不一致视为内容错位
        existing = state.by_position.get(entry.position)
        if existing is not None and existing.key != entry.key:
            inspection.issues.append(PageIssue(
                IssueCode.ENTRY_MISALIGNED,
                (f"第 {entry.position} 条原前缀键为 {existing.key}，重采数据键为 {entry.key}，"
                 "拒绝覆盖并保留原前缀"),
                position=entry.position,
                detail={"expected": existing.key, "received": entry.key},
            ))
            return
        state.by_position[entry.position] = entry
        state.delivered_keys.add(entry.key)
        self._key_index[entry.key] = (state.page_no, entry.position)

    # ------------------------------------------------------------------ #
    # 重试关联
    # ------------------------------------------------------------------ #
    def apply_retry(self, payload: RawPayload) -> RetryOutcome:
        """消费一次重试响应：完整前缀去重、待补采位置按原页序关联补齐。"""
        if payload.page_no not in self._pages:
            # 没有首次采集的页直接按首次接入处理
            self.ingest(payload)
            state = self._pages[payload.page_no]
            outcome = RetryOutcome(
                attempt=payload.attempt,
                page_no=payload.page_no,
                newly_delivered=sorted(state.delivered_keys),
                page_complete=state.page_complete,
            )
            state.retry_outcomes.append(outcome)
            return outcome

        state = self._pages[payload.page_no]
        inspection = inspect_payload(payload, key_field=self.key_field)
        state.history.append(inspection)
        if inspection.declared_total is not None:
            state.last_declared_total = inspection.declared_total
        # 声明条数只允许扩大：重试响应可能只回部分页（如只发重发前缀+少量补采），
        # 本页条数声明变小不能收缩整页的待补采范围，否则页尾条目会被误判越界。
        if inspection.declared_count is not None:
            if state.declared_count is None or inspection.declared_count > state.declared_count:
                state.declared_count = inspection.declared_count

        outcome = RetryOutcome(attempt=payload.attempt, page_no=payload.page_no)
        pending = set(state.missing_positions())
        if state.partial_position is not None:
            # 断条位置及其之后的整段页尾都是待补采区间，不受重试响应本页声明收缩影响
            pending.update(range(state.partial_position, (state.declared_count or state.partial_position) + 1))

        for entry in inspection.complete_entries:
            if entry.key is None:
                continue
            if entry.key in state.delivered_keys:
                # 重发的完整前缀：确认与原前缀同页同位置后去重，不二次交付
                owner = self._key_index.get(entry.key)
                if owner != (state.page_no, entry.position):
                    outcome.issues.append(PageIssue(
                        IssueCode.ENTRY_MISALIGNED,
                        f"重采键 {entry.key} 的页内位置 {entry.position} 与原接入位置 {owner} 不一致",
                        position=entry.position,
                        detail={"key": entry.key, "retry_position": entry.position, "owner": owner},
                    ))
                if entry.key not in outcome.duplicates_skipped:
                    outcome.duplicates_skipped.append(entry.key)
                continue
            if entry.position not in pending:
                outcome.issues.append(PageIssue(
                    IssueCode.OUT_OF_PAGE_ORDER,
                    (f"重采第 {entry.position} 条（键 {entry.key}）越过待补采位置，"
                     "该位置已有完整前缀，拒绝错位写入"),
                    position=entry.position,
                    detail={"key": entry.key},
                ))
                continue
            before = len(state.delivered_keys)
            self._adopt_entry(state, entry, inspection)
            if len(state.delivered_keys) > before:
                outcome.newly_delivered.append(entry.key)  # type: ignore[union-attr]
                pending.discard(entry.position)
                if state.partial_position == entry.position:
                    # 残片曾辨认出键提示：同位置送来不同键属于内容错位信号，
                    # 重采数据作为权威新值仍交付，但必须留痕供结算核对。
                    if (
                        state.partial_key_hint is not None
                        and state.partial_key_hint != entry.key
                    ):
                        outcome.issues.append(PageIssue(
                            IssueCode.ENTRY_MISALIGNED,
                            (f"第 {entry.position} 条断条残片键提示为 {state.partial_key_hint}，"
                             f"重采送达键为 {entry.key}，按重采值交付并标记错位"),
                            position=entry.position,
                            detail={"expected_hint": state.partial_key_hint, "received": entry.key},
                        ))
                    state.partial_position = None
                    state.partial_key_hint = None

        # 重试本身仍截断：保留新的断条位置（位置应与挂起位置一致或更靠后）
        if inspection.partial_entry is not None:
            state.partial_position = inspection.partial_entry.position
            if inspection.partial_entry.expected_key_hint:
                state.partial_key_hint = inspection.partial_entry.expected_key_hint

        outcome.still_missing_positions = state.missing_positions()
        # 重试响应自身的结构发现（总数漂移、断条、键异常等）全部带到关联结果，
        # 总数漂移仍只是告警，不影响 page_complete 的判定。
        outcome.issues.extend(inspection.issues)
        state.page_complete = (
            state.partial_position is None
            and not state.missing_positions()
        )
        outcome.page_complete = state.page_complete
        state.retry_outcomes.append(outcome)
        return outcome

    # ------------------------------------------------------------------ #
    # 装运放行查询（重试回来前也要能查）
    # ------------------------------------------------------------------ #
    def releasable_waybills(self, page_no: int | None = None) -> list[dict[str, Any]]:
        """当前已可放行的运单：已接入且带稳定键的完整事件，每键恰好一条。"""
        result: list[dict[str, Any]] = []
        pages = [self._pages[page_no]] if page_no is not None and page_no in self._pages else list(self._pages.values())
        for state in sorted(pages, key=lambda s: s.page_no):
            for pos in sorted(state.by_position):
                entry = state.by_position[pos]
                result.append({
                    "key": entry.key,
                    "page_no": state.page_no,
                    "position": pos,
                    "status": "可放行",
                })
        return result

    def pending_positions(self, page_no: int) -> dict[str, Any]:
        """仍在等页尾的位置：断条位置 + 丢失位置，供补采定位。"""
        state = self._pages.get(page_no)
        if state is None:
            return {"page_no": page_no, "found": False, "partial_position": None, "missing_positions": []}
        return {
            "page_no": page_no,
            "found": True,
            "partial_position": state.partial_position,
            "partial_key_hint": state.partial_key_hint,
            "missing_positions": state.missing_positions(),
            "page_complete": state.page_complete,
        }

    # ------------------------------------------------------------------ #
    # 结算整页对账（按原页序 + 键值）
    # ------------------------------------------------------------------ #
    def reconcile_page(self, page_no: int) -> dict[str, Any]:
        state = self._pages.get(page_no)
        if state is None:
            return {"page_no": page_no, "found": False}
        ordered: list[dict[str, Any]] = []
        for pos in sorted(state.by_position):
            entry = state.by_position[pos]
            ordered.append({"position": pos, "key": entry.key, "data": entry.data})
        # 键值连续性：按原页序检查位置是否 1..N 连续、键有无重复
        positions = sorted(state.by_position)
        gaps = [p for p in range(1, (state.declared_count or (positions[-1] if positions else 0)) + 1)
                if p not in state.by_position]
        all_keys = [e.key for e in state.by_position.values()]
        return {
            "page_no": page_no,
            "found": True,
            "declared_count": state.declared_count,
            "declared_total_latest": state.last_declared_total,
            "delivered_count": len(state.delivered_keys),
            "unique_key_count": len(set(all_keys)),
            "page_complete": state.page_complete,
            "partial_position": state.partial_position,
            "gaps": gaps,
            "ordered_entries": ordered,
            "retry_history": [o.to_dict() for o in state.retry_outcomes],
            "inspection_history": [
                {"page_no": h.page_no, "status": h.status.value,
                 "received_complete": h.received_complete, "issue_count": len(h.issues)}
                for h in state.history
            ],
        }

    def overview(self) -> dict[str, Any]:
        pages = []
        for state in sorted(self._pages.values(), key=lambda s: s.page_no):
            pages.append({
                "page_no": state.page_no,
                "declared_count": state.declared_count,
                "delivered_count": len(state.delivered_keys),
                "page_complete": state.page_complete,
                "partial_position": state.partial_position,
                "missing_positions": state.missing_positions(),
                "latest_total": state.last_declared_total,
            })
        return {
            "key_field": self.key_field,
            "pages": pages,
            "total_delivered": sum(len(s.delivered_keys) for s in self._pages.values()),
            "releasable_keys": sorted(self._key_index),
        }
