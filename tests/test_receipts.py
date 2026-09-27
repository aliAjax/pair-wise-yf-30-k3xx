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

    def tearDown(self):
        self.tmp.cleanup()

    def make_report(self, dedupe="rc-1", region="CN", country="CN"):
        case = self.svc.create_case(
            "reporter-a", "reporter", region,
            {"patient_ref": "P-1", "region": region, "product": "DrugA", "event_term": "皮疹",
             "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow())},
        )["case"]
        report = self.svc.create_report(case["id"], f"lead-{region.lower()}", "regional_lead", region, {"country": country})
        return case, report

    def submit(self, report, region="CN", **body):
        return self.svc.submit_report(report["id"], f"lead-{region.lower()}", "regional_lead", region, body)

    def reject(self, report, **overrides):
        body = {"decision": "rejected", "reason": "缺少实验室检查",
                "correction_due_at": iso(utcnow() + timedelta(days=14))}
        body.update(overrides)
        return self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN", body)

    def test_submit_enters_awaiting_receipt(self):
        _, report = self.make_report()
        submitted = self.submit(report)
        self.assertEqual(submitted["report"]["status"], "submitted")
        self.assertIsNotNone(submitted["report"]["receipt_due_at"])
        detail = self.svc.report_detail(report["id"], "regional_lead", "CN")
        self.assertEqual(len(detail["submissions"]), 1)
        self.assertEqual(detail["submissions"][0]["attempt"], 1)
        self.assertEqual(detail["receipts"], [])

    def test_acceptance_records_regulator_and_locks_content(self):
        case, report = self.make_report()
        self.submit(report)
        result = self.svc.record_receipt(
            report["id"], "lead-cn", "regional_lead", "CN",
            {"decision": "accepted", "regulator_ref": "NMPA-2026-0001", "decided_at": iso(utcnow())},
        )
        accepted = result["report"]
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["regulator_ref"], "NMPA-2026-0001")
        self.assertIsNotNone(accepted["accepted_at"])
        self.assertEqual(accepted["locked"], 1)
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_followup(case["id"], "reporter-a", "reporter", "CN",
                                  {"content": "锁定后随访", "source": "email", "expected_revision": 1})
        self.assertEqual(ctx.exception.code, "case_locked")
        with self.assertRaises(ApiError) as ctx:
            self.svc.medical_review(case["id"], "reviewer-1", "medical_reviewer",
                                    {"expected_revision": 1, "serious": True, "fatal": False,
                                     "causality": "related", "rationale": "x", "received_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "case_locked")
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"decision": "accepted", "regulator_ref": "AGAIN"})
        self.assertEqual(ctx.exception.code, "report_locked")
        with self.assertRaises(ApiError) as ctx:
            self.submit(report)
        self.assertEqual(ctx.exception.code, "report_locked")
        detail = self.svc.report_detail(report["id"], "global_admin", "")
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertEqual(detail["receipts"][0]["decision"], "accepted")

    def test_rejection_and_resubmission_cycle(self):
        case, report = self.make_report()
        self.submit(report)
        rejected = self.reject(report)["report"]
        self.assertEqual(rejected["status"], "correction_pending")
        self.assertEqual(rejected["rejection_reason"], "缺少实验室检查")
        self.assertIsNotNone(rejected["correction_due_at"])
        with self.assertRaises(ApiError) as ctx:
            self.submit(report)
        self.assertEqual(ctx.exception.code, "report_awaiting_correction")
        with self.assertRaises(ApiError) as ctx:
            self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.assertEqual(ctx.exception.code, "correction_note_required")
        resubmitted = self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN",
                                               {"correction_note": "已补充化验单"})
        self.assertEqual(resubmitted["report"]["status"], "submitted")
        self.assertEqual(resubmitted["report"]["country"], "CN")
        self.assertEqual(resubmitted["attempt"], 2)
        self.assertFalse(resubmitted["correction_late"])
        detail = self.svc.report_detail(report["id"], "regional_lead", "CN")
        self.assertEqual(len(detail["submissions"]), 2)
        self.assertEqual(detail["submissions"][1]["note"], "已补充化验单")
        self.assertEqual(len(detail["receipts"]), 1)
        self.assertEqual(detail["receipts"][0]["decision"], "rejected")
        accepted = self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                           {"decision": "accepted", "regulator_ref": "NMPA-2026-0002"})
        self.assertEqual(accepted["report"]["status"], "accepted")
        case_detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(case_detail["reports"][0]["submissions"]), 2)
        self.assertEqual(len(case_detail["reports"][0]["receipts"]), 2)

    def test_receipt_validation(self):
        _, report = self.make_report()
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN",
                                    {"decision": "accepted", "regulator_ref": "X"})
        self.assertEqual(ctx.exception.code, "report_not_submitted")
        self.submit(report)
        cases = [
            ({"decision": "maybe"}, "invalid_decision"),
            ({"decision": "accepted"}, "regulator_ref_required"),
            ({"decision": "rejected", "correction_due_at": iso(utcnow() + timedelta(days=7))}, "reason_required"),
            ({"decision": "rejected", "reason": "资料不全"}, "correction_due_required"),
            ({"decision": "rejected", "reason": "资料不全",
              "decided_at": iso(utcnow()), "correction_due_at": iso(utcnow() - timedelta(days=1))},
             "correction_due_invalid"),
        ]
        for body, code in cases:
            with self.subTest(code=code), self.assertRaises(ApiError) as ctx:
                self.svc.record_receipt(report["id"], "lead-cn", "regional_lead", "CN", body)
            self.assertEqual(ctx.exception.code, code)

    def test_overdue_receipt_reminder_and_region_scope(self):
        _, report = self.make_report()
        past = iso(utcnow() - timedelta(days=1))
        self.submit(report, receipt_due_at=past)
        cn_list = self.svc.awaiting_receipt_overdue("regional_lead", "CN")
        self.assertEqual([r["id"] for r in cn_list], [report["id"]])
        self.assertEqual(self.svc.awaiting_receipt_overdue("regional_lead", "US"), [])
        state = self.svc.state("regional_lead", "CN")
        self.assertEqual([r["id"] for r in state["overdue_receipts"]], [report["id"]])
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "lead-us", "regional_lead", "US",
                                    {"decision": "accepted", "regulator_ref": "FDA-1"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_receipt(report["id"], "reporter-a", "reporter", "CN",
                                    {"decision": "accepted", "regulator_ref": "X"})
        self.assertEqual(ctx.exception.status, 403)
        accepted = self.svc.record_receipt(report["id"], "admin", "global_admin", "",
                                           {"decision": "accepted", "regulator_ref": "NMPA-2026-0003"})
        self.assertEqual(accepted["report"]["status"], "accepted")
        self.assertEqual(self.svc.awaiting_receipt_overdue("regional_lead", "CN"), [])

    def test_late_correction_and_country_mismatch(self):
        _, report = self.make_report()
        self.submit(report)
        self.reject(report, correction_due_at=iso(utcnow() + timedelta(days=1)))
        late = self.svc.resubmit_report(
            report["id"], "lead-cn", "regional_lead", "CN",
            {"correction_note": "补充资料", "submitted_at": iso(utcnow() + timedelta(days=2))},
        )
        self.assertTrue(late["correction_late"])
        self.reject(report)
        with self.assertRaises(ApiError) as ctx:
            self.svc.resubmit_report(report["id"], "lead-cn", "regional_lead", "CN",
                                     {"correction_note": "改投他国", "country": "US"})
        self.assertEqual(ctx.exception.code, "country_mismatch")


if __name__ == "__main__":
    unittest.main()
