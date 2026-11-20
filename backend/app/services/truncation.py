"""采集载荷截断明细服务：管理多组接入通道，串起截断校验与重采对账。

与平台其他模块的 CRUD 服务不同，这里维护的是有状态的采集会话：
- 一个 channel_code 对应一个 IngestionChannel（一组按页采集的装运事件）；
- 首次采集走 register_payload，重试走 register_retry；
- 装运放行查询与结算对账直接读通道累积状态，重试回来前也能查。
"""
from __future__ import annotations

from typing import Any

from app.truncation.models import RawPayload
from app.truncation.reconcile import IngestionChannel


class TruncationService:
    def __init__(self) -> None:
        self._channels: dict[str, IngestionChannel] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def _get_or_create(self, channel_code: str, key_field: str | None) -> IngestionChannel:
        if channel_code not in self._channels:
            self._channels[channel_code] = IngestionChannel(key_field=key_field or "waybill_no")
            self._meta[channel_code] = {"channel_code": channel_code, "key_field": key_field or "waybill_no"}
        return self._channels[channel_code]

    def list_channels(self) -> list[dict[str, Any]]:
        result = []
        for code, channel in self._channels.items():
            overview = channel.overview()
            result.append({**self._meta[code], **overview})
        return result

    def register_payload(
        self,
        *,
        channel_code: str,
        page_no: int,
        raw_text: str,
        key_field: str | None = None,
        attempt: int = 1,
        request_declared_count: int | None = None,
        request_declared_total: int | None = None,
    ) -> dict[str, Any]:
        """登记一份首次采集响应，返回该页的截断明细。"""
        channel = self._get_or_create(channel_code, key_field)
        payload = RawPayload(
            page_no=page_no,
            raw_text=raw_text,
            attempt=attempt,
            request_declared_count=request_declared_count,
            request_declared_total=request_declared_total,
        )
        inspection = channel.ingest(payload)
        return inspection.to_dict()

    def register_retry(
        self,
        *,
        channel_code: str,
        page_no: int,
        raw_text: str,
        attempt: int,
        key_field: str | None = None,
        request_declared_count: int | None = None,
        request_declared_total: int | None = None,
    ) -> dict[str, Any]:
        """登记一次重试响应，返回与截断前缀关联后的去重/补齐结果。"""
        channel = self._get_or_create(channel_code, key_field)
        payload = RawPayload(
            page_no=page_no,
            raw_text=raw_text,
            attempt=attempt,
            request_declared_count=request_declared_count,
            request_declared_total=request_declared_total,
        )
        outcome = channel.apply_retry(payload)
        return outcome.to_dict()

    def releasable(self, channel_code: str, page_no: int | None = None) -> dict[str, Any]:
        """装运放行视图：可放行运单（每键一条）与等待页尾的位置分开返回。"""
        channel = self._channels.get(channel_code)
        if channel is None:
            return {"channel_code": channel_code, "found": False,
                    "releasable": [], "pending_pages": []}
        page_nos = [page_no] if page_no is not None else channel.page_numbers()
        return {
            "channel_code": channel_code,
            "found": True,
            "releasable": channel.releasable_waybills(page_no),
            "pending_pages": [channel.pending_positions(p) for p in page_nos],
        }

    def reconcile(self, channel_code: str, page_no: int) -> dict[str, Any]:
        """结算视图：按原页序与键值核齐整页。"""
        channel = self._channels.get(channel_code)
        if channel is None:
            return {"channel_code": channel_code, "found": False}
        report = channel.reconcile_page(page_no)
        report["channel_code"] = channel_code
        return report

    def overview(self, channel_code: str) -> dict[str, Any]:
        channel = self._channels.get(channel_code)
        if channel is None:
            return {"channel_code": channel_code, "found": False}
        return {"channel_code": channel_code, **channel.overview()}

    def reset(self, channel_code: str | None = None) -> None:
        """测试或重开采集批次时清空状态。"""
        if channel_code is None:
            self._channels.clear()
            self._meta.clear()
        else:
            self._channels.pop(channel_code, None)
            self._meta.pop(channel_code, None)


# 路由层共享的单例，与平台内存仓库的定位一致
truncation_service = TruncationService()
