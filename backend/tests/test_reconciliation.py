import json
import unittest
from pathlib import Path

from reconciliation import amount, candidates, enforce_auto_post_safety, name


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


if __name__ == "__main__":
    unittest.main()
