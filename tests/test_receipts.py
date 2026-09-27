import sys
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow


class ReceiptFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def make_report(self, country="CN"):
        self.counter += 1
        case = self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": "DrugA", "event_term": "肝损伤",
             "source": "email", "dedupe_key": f"intake-{self.counter}",
             "received_at": iso(utcnow() - timedelta(days=1)), "serious": False},
        )["case"]
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": country})
        return case, report

    def submit(self, report_id, **body):
        return self.svc.submit_report(report_id, "lead-cn", "regional_lead", "CN", body or {})["report"]

    def test_acceptance_completes_and_locks_report(self):
        _, report = self.make_report()
        submitted = self.submit(report["id"])
        self.assertEqual(submitted["status"], "awaiting_receipt")
        self.assertIsNotNone(submitted["receipt_due_at"])
        again = self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertTrue(again["idempotent"])
        result = self.svc.record_receipt(
            report["id"], "lead-cn", "regional_lead", "CN",
            {"outcome": "accepted", "authority_ref": "NMPA-2026-0001", "occurred_at": iso(utcnow())},
        )
        self.assertEqual(result["report"]["status"], "accepted")
        self.assertEqual(result["report"]["locked"], 1)
        self.assertEqual(result["report"]["authority_ref"], "NMPA-2026-0001")
        self.assertIsNotNone(result["report"]["accepted_at"])
        with self.assertRaises(ApiError) as ctx:
            self.submit(report["id"])
        self.assertEqual(ctx.exception.code, "report_locked")
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"outcome": "accepted", "authority_ref": "X"})
        self.assertEqual(ctx.exception.code, "report_locked")

    def test_rejection_returns_to_correction_and_resubmits_same_country(self):
        case, report = self.make_report()
        self.submit(report["id"])
        rejected = self.svc.record_receipt(
            report["id"], "lead-cn", "regional_lead", "CN",
            {"outcome": "rejected", "reason": "缺少实验室检查附件",
             "correction_due_at": iso(utcnow() + timedelta(days=30))},
        )
        self.assertEqual(rejected["report"]["status"], "correction_pending")
        self.assertEqual(rejected["receipt"]["reason"], "缺少实验室检查附件")
        resubmitted = self.submit(report["id"])
        self.assertEqual(resubmitted["status"], "awaiting_receipt")
        detail = self.svc.report_detail(report["id"], "regional_lead", "CN")
        self.assertEqual([s["attempt"] for s in detail["submissions"]], [1, 2])
        self.assertEqual(len(detail["receipts"]), 1)
        self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                {"outcome": "accepted", "authority_ref": "NMPA-009"})
        detail = self.svc.report_detail(report["id"], "regional_lead", "CN")
        self.assertEqual(len(detail["submissions"]), 2)
        self.assertEqual(len(detail["receipts"]), 2)
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.assertEqual(ctx.exception.code, "report_exists")

    def test_receipt_validation(self):
        _, report = self.make_report()
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"outcome": "accepted", "authority_ref": "A"})
        self.assertEqual(ctx.exception.code, "receipt_not_expected")
        self.submit(report["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN", {"outcome": "accepted"})
        self.assertEqual(ctx.exception.code, "authority_ref_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"outcome": "rejected", "correction_due_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"outcome": "rejected", "reason": "缺附件"})
        self.assertEqual(ctx.exception.code, "missing_time")
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN", {"outcome": "unknown"})
        self.assertEqual(ctx.exception.code, "invalid_outcome")

    def test_receipt_region_and_role_permissions(self):
        _, report = self.make_report()
        self.submit(report["id"])
        for actor, role, region in [("r", "reporter", "CN"), ("m", "medical_reviewer", ""),
                                    ("lead-us", "regional_lead", "US")]:
            with self.assertRaises(ApiError) as ctx:
                self.svc.record_receipt(report["id"], actor, role, region,
                                        {"outcome": "accepted", "authority_ref": "A"})
            self.assertEqual(ctx.exception.status, 403)
        ok = self.svc.record_receipt(report["id"], "admin", "global_admin", "",
                                     {"outcome": "accepted", "authority_ref": "A"})
        self.assertEqual(ok["report"]["status"], "accepted")

    def test_overdue_receipts_reminder_scoped_by_region(self):
        _, report = self.make_report()
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN",
                               {"submitted_at": iso(utcnow() - timedelta(days=40)),
                                "receipt_window_days": 30})
        self.assertEqual([r["id"] for r in self.svc.receipts_overdue("regional_lead", "CN")], [report["id"]])
        self.assertEqual(self.svc.receipts_overdue("regional_lead", "US"), [])
        state = self.svc.state("regional_lead", "CN")
        self.assertEqual([r["id"] for r in state["receipts_overdue"]], [report["id"]])
        # 待回执报告不再出现在提交逾期列表中
        self.assertEqual(self.svc.overdue("regional_lead", "CN"), [])
        self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                {"outcome": "accepted", "authority_ref": "A"})
        self.assertEqual(self.svc.receipts_overdue("regional_lead", "CN"), [])

    def test_rejected_resubmission_late_against_correction_deadline(self):
        _, report = self.make_report()
        self.submit(report["id"])
        correction_due = utcnow() - timedelta(days=1)
        self.svc.record_receipt(
            report["id"], "lead-cn", "regional_lead", "CN",
            {"outcome": "rejected", "reason": "材料不全", "correction_due_at": iso(correction_due)},
        )
        resubmitted = self.submit(report["id"])
        self.assertEqual(resubmitted["status"], "awaiting_receipt")
        self.assertEqual(resubmitted["late"], 1)


if __name__ == "__main__":
    unittest.main()
