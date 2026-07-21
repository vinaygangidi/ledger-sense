# Uniquely

An OpenAI/GPT-5.6 demo that reconciles synthetic bank payments against open AR and produces auditable posting instructions.

**Live demo:** [frontend-jade-nu-15.vercel.app](https://frontend-jade-nu-15.vercel.app)

The production demo uses a hosted FastAPI backend. Its OpenAI key is stored only as a deployment environment variable; it is never committed to this repository.

## Why this isn't just another cash application module

Modern ERPs such as SAP and Oracle Fusion already automate the easy matches: exact invoice references, exact amounts, and known customers. Where they struggle is the ambiguous 15–20% of payments that require real judgment—factoring relationships, DBA aliases, truncated names, and other cases that do not fit a pre-configured rule. Those cases typically land in a manual exception queue with little explanation of why.

Ledger Sense is built specifically for that gap. Instead of a fixed rule engine, GPT-5.6 reasons through ambiguous cases the way an experienced AR analyst would and explains every decision in plain language. It is not a replacement for an ERP ledger or workflow; it is designed to sit alongside one, resolving the cases that traditional rule-based matching cannot confidently handle with full audit traceability.

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

### Architecture reference

For the full current design, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The implementation source of truth is [`backend/reconciliation.py`](backend/reconciliation.py): it contains the five-stage async pipeline, deterministic matching, GPT-5.6 prompts, routing hard gates, and the append-only SQLite audit journal. [`backend/main.py`](backend/main.py) exposes the FastAPI/SSE endpoints, while [`frontend/app/page.js`](frontend/app/page.js) consumes SSE events and renders the input, ledger, and posting-decision views.

The runtime flow is: **Next.js dashboard → `POST /analyze` → FastAPI SSE stream → five-agent pipeline → SQLite audit events → posting instructions displayed in the dashboard**. GPT-5.6 is used only for entity resolution and judgment/routing; code verifies all financial math and enforces the final safety controls.

### Demo observability

During a live run, server logs contain concise GPT-5.6 call and response events, such as the transaction ID, a masked payer preview, the resolved relationship, route, and confidence. Logs deliberately exclude full payer names, remittance details, prompts, response bodies, and secrets.

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

The dashboard loads sample 04 by default and includes a **Demo dataset** picker for all ten scenarios. Click **Run synthetic demo** to see each stage progress in real time, compare raw bank payments with reconciled posting decisions, and inspect the resulting audit run through the backend API.

A judge will see verified invoice allocations for high-confidence matches, safe review routing for unresolved cases, and a clear record of why each decision was made.
