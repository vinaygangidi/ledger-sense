# AR Reconciliation Copilot

An OpenAI/GPT-5.6 demo that reconciles synthetic bank payments against open AR and produces auditable posting instructions.

## The problem

Cash application is rarely an exact invoice-number lookup. A bank deposit may have a truncated payer name, a DBA or parent-company name, a factoring intermediary, multiple invoices in one payment, a partial amount, or a disputed invoice. Compliance holds add another constraint: a plausible match must still not post automatically.

These cases force finance teams to combine data cleanup, accounting math, policy checks, and judgment—often manually.

## How it works

The pipeline runs five sequential stages:

1. **Normalize payments** — validates amounts and currencies, standardizes payer names, and extracts remittance references.
2. **Index open AR** — prepares invoice, customer, alias, PO, currency, and status data for matching.
3. **Verify candidates** — code evaluates exact, partial, and multi-invoice candidates using Python `Decimal`; allocations must balance exactly.
4. **Reason about exceptions** — GPT-5.6 receives only pre-validated ambiguous candidates and returns a structured route: auto-post, review, dispute, or compliance hold.
5. **Generate postings** — emits posting instructions only after deterministic allocation and policy checks pass.

### Architecture Notes

Financial math stays in deterministic code because a model must never invent an invoice allocation, exchange an amount, or silently accept a rounding error. GPT-5.6 contributes judgment where it is useful: explaining ambiguous evidence and choosing the safest operational route.

The SQLite audit journal is append-only. Each input, candidate set, model decision, policy override, and posting instruction becomes a new event; database triggers reject updates and deletes. That preserves the traceability and control history a real AR team needs for review and compliance.

## How Codex was used

This project was built with Codex under explicit product direction.

You directed the OpenAI-only stack, GPT-5.6 usage, the AR reconciliation domain, the need for an append-only local audit trail, and the distinction between deterministic matching and model judgment. You also required an honest reset after an earlier implementation followed old Azure-oriented patterns.

Codex independently chose the detailed deterministic-vs-model boundary, the five-stage sequencing, `Decimal` arithmetic for allocations, SQLite triggers to enforce append-only audit records, confidence scoring, the SSE event shape, and the compact before/after dashboard. It also designed the ledger-grounded GPT-5.6 entity-resolution prompt: payer names are resolved only against the supplied customer and alias catalog, with a proposed relationship, confidence, and rationale. GPT-5.6 also generates the analyst-readable routing rationale displayed for every result.

Verification caught two real defects: a deliberately weak-evidence payer was correctly unresolved but initially received 99% confidence, so Codex added explicit evidence bands to the prompt and caps unresolved results at 35%; and a simplistic `HOLD` string check misclassified `OMEGA GLOBAL HOLDINGS` as a compliance hold, so it now recognizes only explicit compliance, sanctions, or legal hold phrases.

Verification also exposed a critical posting-safety gap: GPT-5.6 could recommend `auto_post` without a verified deterministic match. The root cause was twofold—partially paid invoices were excluded from matching even when they retained an open balance, and the routing layer had no safety gate on the model recommendation. Codex made `PARTIAL` invoices matchable against their remaining balance and now permits auto-posting only after a ≥95% deterministic allocation verification; every unsafe model recommendation is forced to `review`. An adversarial audit of all 10 sample datasets now reports zero unsafe auto-posts.

Codex accelerated repository cleanup, backend and frontend implementation, dependency setup, synthetic-data verification, SSE testing, and fresh-clone setup validation. The first implementation attempt did draw on prior patterns in this workspace; it was deliberately discarded at your direction and rebuilt independently with the standard OpenAI SDK only.

## Quick Start

Prerequisites:

- Python 3.11+
- Node.js 18+
- An OpenAI API key for live GPT-5.6 exception reasoning

Clone and start the backend:

```bash
git clone https://github.com/vinaygangidi/cash-reconciliation-codex.git
cd cash-reconciliation-codex/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=...
uvicorn main:app --reload --port 8000
```

In a second terminal, start the frontend:

```bash
cd cash-reconciliation-codex/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The synthetic demo works without an API key for deterministic matching. Without a key, ambiguous cases safely route to human review; set `OPENAI_API_KEY` to enable GPT-5.6 exception routing.

## Sample data / demo

The repository includes ten synthetic bank-statement and open-AR datasets under `backend/data/samples/`. No real financial data is included.

The dashboard loads sample 01 by default. Click **Run synthetic demo** to see each stage progress in real time, compare raw bank payments with reconciled posting decisions, and inspect the resulting audit run through the backend API.

A judge will see verified invoice allocations for high-confidence matches, safe review routing for unresolved cases, and a clear record of why each decision was made.
