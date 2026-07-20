import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reconciliation import AuditLog, amount, candidates, enforce_auto_post_safety, name, run_pipeline


ROOT = Path(__file__).resolve().parents[1]


def normalized_invoices(ledger):
    return [
        {
            **invoice,
            "open_amount": amount(invoice["open_amount"]),
            "currency": invoice.get("currency", "USD"),
            "payer": name(invoice["customer_name"]),
        }
        for invoice in ledger["invoices"]
    ]


def normalized_payment(payment):
    return {
        **payment,
        "amount": amount(payment["amount"]),
        "currency": payment.get("currency", "USD"),
        "payer": name(payment.get("payer_raw", "")),
        "remittance": payment.get("remittance_text", ""),
    }


class AutoPostSafetyTests(unittest.TestCase):
    def test_partial_invoice_is_matchable_by_remaining_balance(self):
        sample = ROOT / "data" / "samples" / "sample_01"
        bank = json.loads((sample / "bank_statement.json").read_text())
        ledger = json.loads((sample / "open_ar.json").read_text())
        payment = next(p for p in bank["transactions"] if p["txn_id"] == "TXN-01-007")

        matches = candidates(normalized_payment(payment), normalized_invoices(ledger))

        self.assertEqual(matches[0][0], "partial_payment")
        self.assertEqual([invoice["invoice_id"] for invoice in matches[0][1]], ["INV-01-008"])

    def test_model_cannot_auto_post_without_a_verified_allocation(self):
        decision = enforce_auto_post_safety(
            {"route": "auto_post", "confidence": .99, "reason": "Model recommendation."},
            [],
        )

        self.assertEqual(decision["route"], "review")
        self.assertLessEqual(decision["confidence"], .45)

    def test_model_cannot_auto_post_a_low_confidence_partial_match(self):
        verified = [("partial_payment", [{"invoice_id": "INV-01-008"}], .82)]
        decision = enforce_auto_post_safety(
            {"route": "auto_post", "confidence": .99, "reason": "Model recommendation."},
            verified,
        )

        self.assertEqual(decision["route"], "review")


class PipelineStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_exception_stage_completes_after_all_postings(self):
        sample = ROOT / "data" / "samples" / "sample_01"
        bank = json.loads((sample / "bank_statement.json").read_text())
        ledger = json.loads((sample / "open_ar.json").read_text())

        async def resolved_entity(*_):
            return {"resolved_entity": None, "relationship": "unresolved", "confidence": 0, "rationale": "test"}

        async def route_review(*_):
            return {"route": "review", "confidence": .4, "reason": "test"}

        with tempfile.TemporaryDirectory() as tmp, patch("reconciliation.resolve_entity", resolved_entity), patch("reconciliation.reason", route_review):
            audit = AuditLog(Path(tmp) / "audit.sqlite3")
            await audit.initialize()
            events = [event async for event in run_pipeline("test-run", bank, ledger, audit)]

        exception_complete = [event for event in events if event.get("stage") == "exception_reasoning" and event.get("status") == "complete"]
        posting_events = [event for event in events if event.get("stage") == "posting" and event.get("output")]

        self.assertEqual(len(exception_complete), 1)
        self.assertEqual(exception_complete[0]["count"], len(posting_events))
        self.assertLess(events.index(exception_complete[0]), len(events) - 1)


if __name__ == "__main__":
    unittest.main()
