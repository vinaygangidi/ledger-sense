import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reconciliation import AuditLog, amount, amount_facts, candidates, deterministic_policy, enforce_auto_post_safety, name, reason, run_pipeline


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

    def test_generic_sample_payers_have_no_deterministic_match_and_are_review_only(self):
        for sample_id, transaction_id in (("04", "TXN-04-008"), ("06", "TXN-06-009")):
            sample = ROOT / "data" / "samples" / f"sample_{sample_id}"
            bank = json.loads((sample / "bank_statement.json").read_text())
            ledger = json.loads((sample / "open_ar.json").read_text())
            payment = next(p for p in bank["transactions"] if p["txn_id"] == transaction_id)

            matches = candidates(normalized_payment(payment), normalized_invoices(ledger))
            decision = enforce_auto_post_safety(
                {"route": "auto_post", "confidence": .99, "reason": "unsafe model output"},
                matches,
            )

            self.assertEqual(matches, [])
            self.assertEqual(decision["route"], "review")


class PipelineStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_routing_payload_excludes_unallowlisted_sensitive_fields(self):
        class FakeResponses:
            input_payload = None
            request_kwargs = None

            async def create(self, **kwargs):
                self.input_payload = kwargs["input"]
                self.request_kwargs = kwargs
                return type("Response", (), {"output_text": '{"route":"review","confidence":0.4,"rationale":"test"}'})()

        class FakeClient:
            def __init__(self):
                self.responses = FakeResponses()

        payment = {
            "txn_id": "TXN-PRIVACY-001", "payer_raw": "Example Payer",
            "remittance_text": "INV-123", "amount": amount("100.00"),
            "currency": "USD", "payment_type": "ACH", "note": "test",
            "statement_date": "2026-07-20", "payer": "EXAMPLE PAYER",
            "remittance": "INV-123", "account_number": "123456789",
            "routing_number": "987654321", "tax_id": "12-3456789",
        }
        client = FakeClient()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch("reconciliation.AsyncOpenAI", return_value=client):
            await reason(payment, [], {"resolved_entity": None}, [])

        sent_payment = json.loads(client.responses.input_payload)["payment"]
        sent_prompt = json.loads(client.responses.input_payload)
        self.assertEqual(set(sent_payment), {
            "txn_id", "payer_raw", "remittance_text", "amount", "currency",
            "payment_type", "note", "statement_date", "payer", "remittance",
        })
        self.assertNotIn("account_number", sent_payment)
        self.assertNotIn("routing_number", sent_payment)
        self.assertNotIn("tax_id", sent_payment)
        self.assertNotIn("temperature", client.responses.request_kwargs)
        self.assertIn("exact numeric confidence formatted to two decimals", sent_prompt["instruction"])

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
        first_posting = next(event["output"] for event in posting_events if event["transaction_id"] == "TXN-01-001")
        self.assertEqual(first_posting["route"], "review")


class EdgeCaseCoverageTests(unittest.TestCase):
    def sample(self, sample_id):
        folder = ROOT / "data" / "samples" / f"sample_{sample_id}"
        return (
            json.loads((folder / "bank_statement.json").read_text()),
            json.loads((folder / "open_ar.json").read_text()),
        )

    def transaction(self, sample_id, transaction_id):
        bank, ledger = self.sample(sample_id)
        payment = next(p for p in bank["transactions"] if p["txn_id"] == transaction_id)
        return normalized_payment(payment), normalized_invoices(ledger), ledger

    def test_amount_and_reference_strategies_are_deterministic(self):
        cases = (
            ("01", "TXN-01-001", "exact_reference"),
            ("01", "TXN-01-002", "multi_invoice"),
            ("01", "TXN-01-003", "early_pay_discount"),
            ("01", "TXN-01-004", "wire_fee_writeoff"),
            ("01", "TXN-01-005", "fifo_amount_match"),
            ("07", "TXN-07-002", "amount_match"),
            ("07", "TXN-07-003", "exact_reference"),
            ("08", "TXN-08-002", "credit_memo_net"),
            ("05", "TXN-05-001", "fx_verified"),
        )
        for sample_id, transaction_id, strategy in cases:
            payment, invoices, ledger = self.transaction(sample_id, transaction_id)
            matches = candidates(payment, invoices, ledger)
            self.assertEqual(matches[0][0], strategy, f"{sample_id}/{transaction_id}")

    def test_exception_policies_block_posting_deterministically(self):
        cases = (
            ("03", "TXN-03-001", "compliance_hold"),
            ("03", "TXN-03-002", "dispute"),
            ("03", "TXN-03-004", "compliance_hold"),
            ("06", "TXN-06-001", "review"),
            ("06", "TXN-06-002", "review"),
            ("06", "TXN-06-003", "review"),
        )
        seen = set()
        for sample_id, transaction_id, route in cases:
            payment, invoices, ledger = self.transaction(sample_id, transaction_id)
            decision = deterministic_policy(payment, invoices, ledger, seen)
            self.assertIsNotNone(decision, f"{sample_id}/{transaction_id}")
            self.assertEqual(decision["route"], route, f"{sample_id}/{transaction_id}")

    def test_duplicate_is_detected_from_batch_history(self):
        payment, invoices, ledger = self.transaction("06", "TXN-06-004")
        seen = set()
        self.assertIsNone(deterministic_policy(payment, invoices, ledger, seen))
        duplicate, _, _ = self.transaction("06", "TXN-06-005")
        decision = deterministic_policy(duplicate, invoices, ledger, seen)
        self.assertEqual(decision["route"], "review")

    def test_overpayment_and_fx_deltas_are_verified_in_code(self):
        payment, invoices, _ = self.transaction("08", "TXN-08-001")
        facts = amount_facts(payment, invoices)
        self.assertIn({"invoice_id": "INV-08-001", "kind": "overpayment", "delta": "4000.00"}, facts)

        payment, invoices, _ = self.transaction("05", "TXN-05-003")
        facts = amount_facts(payment, invoices)
        self.assertTrue(any(fact["kind"] == "fx_conversion" and fact["expected_amount"] == "29601.00" and fact["delta"] == "-1.00" for fact in facts))


if __name__ == "__main__":
    unittest.main()
