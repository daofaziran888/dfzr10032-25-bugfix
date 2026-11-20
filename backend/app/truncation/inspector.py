"""分页响应校验与截断恢复。

校验三件事（对应需求里的“声明条数、消息结束边界、已收到事件”）：
1. 声明条数：响应里 events 的声明长度、请求登记条数、实际解析出的完整条目三方比对；
2. 消息结束边界：外层信封与数组是否完整闭合（区分“干净截断”和“字段中间断开”）；
3. 已收到事件：逐条清点，给出完整前缀、断条的页内位置、丢失位置。

关键口径：服务端声明的 total 会在采集期间因新增记录而变化，total 对不上只产生
TOTAL_DRIFT 告警，绝不据此把一个边界完整的页判成错误页。

解析器只依赖标准库 json.JSONDecoder.raw_decode 做流式恢复，被截断在任意字符处的
响应都能恢复出“完整前缀”，并保留断条残片与字符偏移。
"""
from __future__ import annotations

import json
from typing import Any

from app.truncation.models import (
    EntryRecord,
    IssueCode,
    PageIssue,
    PageInspection,
    PageStatus,
    PartialEntry,
    RawPayload,
)

PREVIEW_LIMIT = 80


def _preview(text: str) -> str:
    text = text.replace("\n", "\\n").replace("\r", "\\r")
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT] + "…"


def _extract_key_hint(fragment: str, key_field: str) -> str | None:
    """从断条残片里尽量辨认稳定键：残片可能只来得及写出键字段。"""
    needle = f'"{key_field}"'
    idx = fragment.find(needle)
    if idx < 0:
        return None
    colon = fragment.find(":", idx + len(needle))
    if colon < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(fragment[colon + 1:].lstrip())
    except json.JSONDecodeError:
        return None
    return str(value) if value is not None else None


def _recover_array(raw_text: str, key_field: str, page_no: int) -> tuple[
    list[EntryRecord], PartialEntry | None, bool, int | None
]:
    """从（可能被截断的）响应原文里恢复 events 数组。

    返回：(完整条目列表, 断条残片或None, 是否找到数组结束括号, 声明总数字段值)。
    采用手工定位 + raw_decode，而不是 json.loads 后补救，这样能拿到断条在原文里的
    字符偏移，并保证字段中间断开时完整前缀一条不丢。
    """
    decoder = json.JSONDecoder()
    array_start = raw_text.find("[")
    if array_start < 0:
        return [], None, False, _scan_total(raw_text)

    idx = array_start + 1
    entries: list[EntryRecord] = []
    partial: PartialEntry | None = None
    array_closed = False
    position = 0

    while idx < len(raw_text):
        # 跳过空白与元素之间的逗号
        while idx < len(raw_text) and raw_text[idx] in " \t\r\n,":
            idx += 1
        if idx >= len(raw_text):
            break  # 截在干净的元素边界上，没有断条
        if raw_text[idx] == "]":
            array_closed = True
            idx += 1
            break
        fragment_start = idx
        try:
            obj, end = decoder.raw_decode(raw_text, idx)
        except json.JSONDecodeError:
            # 末条断在字段中间：保留残片与页内位置（位置 = 下一个应到的序号）
            fragment = raw_text[fragment_start:].rstrip().rstrip(",")
            partial = PartialEntry(
                page_no=page_no,
                position=position + 1,
                raw_fragment=fragment,
                char_offset=fragment_start,
                fragment_preview=_preview(fragment),
                expected_key_hint=_extract_key_hint(fragment, key_field),
            )
            idx = len(raw_text)
            break
        if not isinstance(obj, dict):
            # 非对象元素属于结构错误，直接上抛给调用方判 MALFORMED
            raise json.JSONDecodeError("events 数组元素必须是对象", raw_text, fragment_start)
        position += 1
        key_value = obj.get(key_field)
        # 允许服务端在条目里携带显式页内序号（重采稀疏页时数组下标不等于原页位置）；
        # 缺省仍按数组下标从 1 编号，保证普通分页口径不变。
        explicit_pos = obj.get("_position")
        entry_position = int(explicit_pos) if isinstance(explicit_pos, int) else position
        entries.append(
            EntryRecord(
                page_no=page_no,
                position=entry_position,
                key=None if key_value is None else str(key_value),
                data=obj,
            )
        )
        idx = end

    return entries, partial, array_closed, _scan_total(raw_text)


def _scan_total(raw_text: str) -> int | None:
    """在原文里尽力读取 total 声明；截断可能让该字段本身丢失，返回 None。"""
    for needle in ('"total"', '"declared_total"'):
        idx = raw_text.find(needle)
        if idx < 0:
            continue
        colon = raw_text.find(":", idx + len(needle))
        if colon < 0:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw_text[colon + 1:].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, int):
            return value
    return None


def _scan_declared_count(raw_text: str) -> int | None:
    """读取响应信封里对本页条数的显式声明（count/size 类字段）。"""
    for needle in ('"declared_count"', '"count"', '"page_size"'):
        idx = raw_text.find(needle)
        if idx < 0:
            continue
        colon = raw_text.find(":", idx + len(needle))
        if colon < 0:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(raw_text[colon + 1:].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, int):
            return value
    return None


def inspect_payload(payload: RawPayload, *, key_field: str = "waybill_no") -> PageInspection:
    """校验一份分页响应并生成采集载荷截断明细。"""
    raw = payload.raw_text
    issues: list[PageIssue] = []
    declared_total_resp = _scan_total(raw)
    declared_count_resp = _scan_declared_count(raw)

    # 先试完整解析：成功说明消息边界与全部元素都闭合
    full_parse_ok = False
    try:
        parsed = json.loads(raw)
        full_parse_ok = True
    except json.JSONDecodeError:
        parsed = None

    if full_parse_ok:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("events"), list):
            return PageInspection(
                page_no=payload.page_no,
                status=PageStatus.MALFORMED,
                declared_count=declared_count_resp,
                received_complete=0,
                declared_total=declared_total_resp,
                end_boundary_present=False,
                issues=[PageIssue(IssueCode.MISSING_END_BOUNDARY,
                                  "响应不是包含 events 数组的信封结构")],
                raw_length=len(raw),
            )
        events = parsed["events"]
        entries = [
            EntryRecord(
                page_no=payload.page_no,
                position=int(item["_position"])
                if isinstance(item, dict) and isinstance(item.get("_position"), int)
                else i + 1,
                key=None if item.get(key_field) is None else str(item.get(key_field)),
                data=item,
            )
            for i, item in enumerate(events)
            if isinstance(item, dict)
        ]
        if len(entries) != len(events):
            issues.append(PageIssue(IssueCode.MISSING_END_BOUNDARY, "events 数组中存在非对象元素"))
        partial = None
        array_closed = True
    else:
        try:
            entries, partial, array_closed, recovered_total = _recover_array(raw, key_field, payload.page_no)
            if declared_total_resp is None:
                declared_total_resp = recovered_total
        except json.JSONDecodeError as exc:
            return PageInspection(
                page_no=payload.page_no,
                status=PageStatus.MALFORMED,
                declared_count=declared_count_resp,
                received_complete=0,
                declared_total=declared_total_resp,
                end_boundary_present=False,
                issues=[PageIssue(IssueCode.MISSING_END_BOUNDARY, f"响应结构无法恢复：{exc.msg}")],
                raw_length=len(raw),
            )

    end_boundary_present = full_parse_ok and array_closed

    # ---- 校验 1：消息结束边界 ----
    if not end_boundary_present:
        if partial is not None:
            issues.append(PageIssue(
                IssueCode.MID_FIELD_BREAK,
                f"第 {partial.position} 条在字段中间断开，响应缺少结束边界",
                position=partial.position,
                detail={"char_offset": partial.char_offset, "fragment_preview": partial.fragment_preview},
            ))
        else:
            issues.append(PageIssue(
                IssueCode.MISSING_END_BOUNDARY,
                "响应在完整条目边界处被截断，缺少消息结束边界",
            ))

    # ---- 校验 2：声明条数 vs 实收（请求登记、响应声明、实际完整条目三方） ----
    declared_count = declared_count_resp if declared_count_resp is not None else payload.request_declared_count
    received = len(entries)
    if declared_count is not None and declared_count != received:
        issues.append(PageIssue(
            IssueCode.DECLARED_COUNT_MISMATCH,
            f"声明本页 {declared_count} 条，实际收到完整事件 {received} 条",
            detail={"declared": declared_count, "received_complete": received},
        ))
    if (
        payload.request_declared_count is not None
        and declared_count_resp is not None
        and payload.request_declared_count != declared_count_resp
    ):
        issues.append(PageIssue(
            IssueCode.DECLARED_COUNT_MISMATCH,
            "请求登记条数与响应声明条数不一致",
            detail={"request": payload.request_declared_count, "response": declared_count_resp},
        ))

    # ---- 校验 3：总数漂移（仅告警，不把完整页打成错误） ----
    declared_total = declared_total_resp
    if (
        declared_total is not None
        and payload.request_declared_total is not None
        and declared_total != payload.request_declared_total
    ):
        issues.append(PageIssue(
            IssueCode.TOTAL_DRIFT,
            (f"服务端声明总数由 {payload.request_declared_total} 变为 {declared_total}，"
             "采集期间可能有新增记录，仅告警不判错"),
            detail={
                "request_total": payload.request_declared_total,
                "response_total": declared_total,
            },
        ))

    # ---- 条目丢失定位：声明位置上既没有完整条目也没有断条 ----
    if declared_count is not None:
        occupied = {e.position for e in entries}
        if partial is not None:
            occupied.add(partial.position)
        for pos in range(1, declared_count + 1):
            if pos not in occupied:
                issues.append(PageIssue(
                    IssueCode.ENTRY_LOST,
                    f"页内第 {pos} 条丢失：截断导致该位置没有任何收到的数据",
                    position=pos,
                ))

    # ---- 稳定键检查：缺键不能放行对账；同页键重复是内容错位信号 ----
    seen_keys: dict[str, int] = {}
    for entry in entries:
        if entry.key is None:
            issues.append(PageIssue(
                IssueCode.KEY_MISSING,
                f"第 {entry.position} 条缺少稳定键字段 {key_field}，不能参与键值对账",
                position=entry.position,
            ))
            continue
        if entry.key in seen_keys:
            issues.append(PageIssue(
                IssueCode.KEY_DUPLICATED,
                f"稳定键 {entry.key} 在页内第 {seen_keys[entry.key]} 与第 {entry.position} 条重复，疑似内容错位",
                position=entry.position,
                detail={"key": entry.key, "first_position": seen_keys[entry.key]},
            ))
        else:
            seen_keys[entry.key] = entry.position

    is_truncated = not end_boundary_present or any(
        i.code in (IssueCode.MID_FIELD_BREAK, IssueCode.MISSING_END_BOUNDARY, IssueCode.ENTRY_LOST)
        for i in issues
    )
    status = PageStatus.TRUNCATED if is_truncated else PageStatus.COMPLETE

    return PageInspection(
        page_no=payload.page_no,
        status=status,
        declared_count=declared_count,
        received_complete=received,
        declared_total=declared_total,
        end_boundary_present=end_boundary_present,
        complete_entries=entries,
        partial_entry=partial,
        issues=issues,
        raw_length=len(raw),
    )
