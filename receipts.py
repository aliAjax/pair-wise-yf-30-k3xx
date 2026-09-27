"""回执判定层：监管回执的纯规则校验与状态流转，不依赖数据库和 HTTP。

报告状态机：
    pending/overdue --提交--> submitted（待回执）
    submitted --受理--> accepted（已完成，内容锁定）
    submitted --退件--> correction_pending（待更正）
    correction_pending --补件重提（同一国家）--> submitted
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 报告状态
PENDING = "pending"
OVERDUE = "overdue"
AWAITING_RECEIPT = "submitted"  # 已提交，等待监管回执
ACCEPTED = "accepted"  # 已受理，报告完成并锁定
CORRECTION_PENDING = "correction_pending"  # 已退件，待更正后重提

RECEIPT_WINDOW_DAYS = 30  # 提交后等待监管回执的默认天数

DECISIONS = {"accepted", "rejected"}


class ReceiptRuleError(Exception):
    """回执判定失败：status 对应 HTTP 语义，code 供程序判断。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _parse_time(value: Any, field_name: str, default: datetime | None = None) -> datetime:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ReceiptRuleError("missing_time", f"必须提供 {field_name} 时间")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptRuleError("invalid_time", f"{field_name} 时间格式错误: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class ReceiptOutcome:
    """一次回执判定结果：写库前不可变。"""

    decision: str
    decided_at: datetime
    new_status: str
    locked: bool
    regulator_ref: str | None = None
    reason: str | None = None
    correction_due_at: datetime | None = None


@dataclass
class ResubmissionOutcome:
    """一次补件重提判定结果。"""

    submitted_at: datetime
    note: str
    receipt_due_at: datetime
    correction_late: bool


def receipt_deadline(submitted_at: datetime, explicit: Any = None) -> datetime:
    """提交后期望收到监管回执的期限：显式指定或默认窗口。"""
    if explicit is not None and str(explicit).strip():
        return _parse_time(explicit, "receipt_due_at")
    return submitted_at + timedelta(days=RECEIPT_WINDOW_DAYS)


def is_receipt_overdue(report: dict[str, Any], now: datetime) -> bool:
    """已提交但超过回执期限仍未收到监管结果。"""
    if report.get("status") != AWAITING_RECEIPT or not report.get("receipt_due_at"):
        return False
    return _parse_time(report["receipt_due_at"], "receipt_due_at") < now


def evaluate_receipt(report: dict[str, Any], body: dict[str, Any], now: datetime) -> ReceiptOutcome:
    """判定一次监管回执：校验当前状态与必填项，给出新状态。"""
    status = report.get("status")
    if status == ACCEPTED or report.get("locked"):
        raise ReceiptRuleError("report_locked", "报告已受理并锁定，不能再登记回执", 409)
    if status == CORRECTION_PENDING:
        raise ReceiptRuleError("report_awaiting_correction", "报告已退件，请先补件重提再登记回执", 409)
    if status != AWAITING_RECEIPT:
        raise ReceiptRuleError("report_not_submitted", "报告尚未提交，不能登记回执", 409)
    decision = str(body.get("decision", "")).strip().lower()
    if decision not in DECISIONS:
        raise ReceiptRuleError("invalid_decision", "decision 必须是 accepted 或 rejected")
    decided_at = _parse_time(body.get("decided_at"), "decided_at", now)
    if decision == "accepted":
        regulator_ref = str(body.get("regulator_ref", "")).strip()
        if not regulator_ref:
            raise ReceiptRuleError("regulator_ref_required", "受理时必须填写监管编号 regulator_ref")
        return ReceiptOutcome(decision=decision, decided_at=decided_at, new_status=ACCEPTED, locked=True, regulator_ref=regulator_ref)
    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise ReceiptRuleError("reason_required", "退件时必须填写原因 reason")
    if body.get("correction_due_at") is None or not str(body.get("correction_due_at")).strip():
        raise ReceiptRuleError("correction_due_required", "退件时必须填写补件期限 correction_due_at")
    correction_due = _parse_time(body.get("correction_due_at"), "correction_due_at")
    if correction_due <= decided_at:
        raise ReceiptRuleError("correction_due_invalid", "补件期限必须晚于退件时间")
    return ReceiptOutcome(decision=decision, decided_at=decided_at, new_status=CORRECTION_PENDING, locked=False, reason=reason, correction_due_at=correction_due)


def evaluate_resubmission(report: dict[str, Any], body: dict[str, Any], now: datetime) -> ResubmissionOutcome:
    """判定退件后的补件重提：报告必须处于待更正，且只能重提给同一国家。"""
    if report.get("status") != CORRECTION_PENDING:
        raise ReceiptRuleError("report_not_in_correction", "只有退件待更正的报告可以补件重提", 409)
    country = str(body.get("country", "")).strip().upper()
    if country and country != str(report.get("country", "")).upper():
        raise ReceiptRuleError("country_mismatch", "补件必须重提给同一国家，不能变更国家", 409)
    note = str(body.get("correction_note", "")).strip()
    if not note:
        raise ReceiptRuleError("correction_note_required", "补件重提必须填写修正说明 correction_note")
    submitted_at = _parse_time(body.get("submitted_at"), "submitted_at", now)
    correction_due = report.get("correction_due_at")
    correction_late = bool(correction_due) and submitted_at > _parse_time(correction_due, "correction_due_at")
    return ResubmissionOutcome(
        submitted_at=submitted_at,
        note=note,
        receipt_due_at=receipt_deadline(submitted_at, body.get("receipt_due_at")),
        correction_late=correction_late,
    )
