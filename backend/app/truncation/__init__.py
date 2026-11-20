"""采集载荷截断明细：截断恢复解析、重采关联与按页对账。"""
from app.truncation.inspector import inspect_payload
from app.truncation.models import (
    EntryRecord,
    IssueCode,
    PageIssue,
    PageStatus,
    RawPayload,
)
from app.truncation.reconcile import IngestionChannel, RetryOutcome

__all__ = [
    "inspect_payload",
    "EntryRecord",
    "IssueCode",
    "PageIssue",
    "PageStatus",
    "RawPayload",
    "IngestionChannel",
    "RetryOutcome",
]
