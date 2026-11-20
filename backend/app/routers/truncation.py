"""采集载荷截断明细接口。

把“响应截断校验 → 重采关联去重 → 按原页序键值对账”这条链路开放给前端与外部系统：
- POST /api/truncation/channels/{code}/payloads   登记首次采集响应，回截断明细
- POST /api/truncation/channels/{code}/retries     登记重试响应，回关联/去重结果
- GET  /api/truncation/channels/{code}/releasable  装运放行查询（重试回来前可查）
- GET  /api/truncation/channels/{code}/pages/{p}/reconcile  结算整页对账
- GET  /api/truncation/channels/{code}/overview    通道汇总
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.truncation import truncation_service

router = APIRouter(prefix="/api/truncation", tags=["采集载荷截断明细"])


class PayloadRequest(BaseModel):
    page_no: int
    raw_text: str = Field(..., description="响应体原文，允许在任意字符处被截断")
    key_field: str | None = "waybill_no"
    attempt: int = 1
    request_declared_count: int | None = None
    request_declared_total: int | None = None


class RetryRequest(PayloadRequest):
    attempt: int = 2


@router.get("/channels")
def list_channels() -> dict[str, Any]:
    """列出全部采集通道及其接入进度。"""
    items = truncation_service.list_channels()
    return {"items": items, "total": len(items)}


@router.post("/channels/{channel_code}/payloads")
def register_payload(channel_code: str, req: PayloadRequest) -> dict[str, Any]:
    """登记首次采集响应并生成截断明细（声明条数/结束边界/已收到事件三项校验）。"""
    if not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text 不能为空：没有响应原文无法校验截断")
    return truncation_service.register_payload(
        channel_code=channel_code,
        page_no=req.page_no,
        raw_text=req.raw_text,
        key_field=req.key_field,
        attempt=req.attempt,
        request_declared_count=req.request_declared_count,
        request_declared_total=req.request_declared_total,
    )


@router.post("/channels/{channel_code}/retries")
def register_retry(channel_code: str, req: RetryRequest) -> dict[str, Any]:
    """登记重试响应：完整前缀去重不重复交付，待补采位置按原页序关联。"""
    if not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text 不能为空：没有重试原文无法关联")
    return truncation_service.register_retry(
        channel_code=channel_code,
        page_no=req.page_no,
        raw_text=req.raw_text,
        attempt=req.attempt,
        key_field=req.key_field,
        request_declared_count=req.request_declared_count,
        request_declared_total=req.request_declared_total,
    )


@router.get("/channels/{channel_code}/releasable")
def releasable(
    channel_code: str,
    page_no: int | None = Query(default=None, description="不传则汇总全部页"),
) -> dict[str, Any]:
    """装运放行视图：哪些运单已可放行、哪些位置仍在等页尾。"""
    return truncation_service.releasable(channel_code, page_no)


@router.get("/channels/{channel_code}/pages/{page_no}/reconcile")
def reconcile(channel_code: str, page_no: int) -> dict[str, Any]:
    """结算对账视图：按原页序核齐整页，每键只占一条。"""
    report = truncation_service.reconcile(channel_code, page_no)
    if not report.get("found"):
        raise HTTPException(status_code=404, detail=f"通道 {channel_code} 或第 {page_no} 页不存在")
    return report


@router.get("/channels/{channel_code}/overview")
def overview(channel_code: str) -> dict[str, Any]:
    """通道汇总：各页声明/交付条数、断条位置、丢失位置与最新总数。"""
    data = truncation_service.overview(channel_code)
    if not data.get("found"):
        raise HTTPException(status_code=404, detail=f"通道 {channel_code} 不存在")
    return data
