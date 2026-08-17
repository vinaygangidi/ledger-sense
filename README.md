# Ledger Sense

![Language](https://img.shields.io/badge/language-Python-blue?style=flat-square)
![Last Commit](https://img.shields.io/github/last-commit/vinaygangidi/ledger-sense?style=flat-square)
![Tests](https://img.shields.io/badge/tests-14%20passing-brightgreen?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)

Ledger Sense is an OpenAI and GPT-5.6 demo for reconciling synthetic bank payments against open accounts receivable. It helps an AR team understand who paid, what can safely be applied, and what needs a person to review.

Live demo: [frontend-jade-nu-15.vercel.app](https://frontend-jade-nu-15.vercel.app)

Architecture reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Why Ledger Sense exists

Most ERP cash application tools handle the straightforward work well. They can match a known customer, an exact invoice reference, and an exact amount. The difficult payments are different. A bank statement may contain a shortened payer name, a DBA name, a parent company, a factoring agent, a partial payment, or a disputed invoice.

Those cases often become a manual exception queue. The analyst has to interpret messy context, check the numbers, understand policy, and leave a record of why the payment was handled a certain way.

Ledger Sense is built for that gap. GPT-5.6 helps interpret the ambiguity. Deterministic Python code verifies the financial math. Hard policy gates make sure a model recommendation cannot bypass a compliance hold or create an unsafe posting.

## What the application does

The application runs five stages for each payment.

1. Normalize payments. The backend validates amounts and currencies, standardizes payment data, and asks GPT-5.6 to resolve ambiguous payer and entity names from the supplied ledger catalog.
2. Index open AR. The backend prepares invoices, aliases, relationships, purchase orders, currencies, and invoice status for matching.
3. Verify candidate matches. Python `Decimal` code evaluates exact, partial, multi-invoice, discount, credit memo, fee, and FX cases.
4. Reason about exceptions. GPT-5.6 receives grounded evidence and produces a route and an analyst-readable rationale.
5. Create posting instructions. The backend emits a posting only after deterministic allocation and policy checks pass.

The dashboard shows the raw bank statement beside the open AR ledger. It streams each stage in real time and then shows the final posting decision, entity resolution, confidence, and rationale for every transaction.

## Safety and auditability

Financial math stays in deterministic code. A language model must not invent an invoice allocation, accept an incorrect amount, or silently approve a rounding difference.

GPT-5.6 is used where language judgment is useful. It resolves ambiguous payer identities, considers DBA and factoring relationships, and explains the safest route for a case.

The backend enforces hard gates. An auto-post requires a code-verified allocation with at least 95 percent confidence. Compliance and legal holds, disputed invoices, duplicates, NSF returns, post-dated checks, and stale checks are enforced in code.

Every stage appends an event to the SQLite audit journal. Database triggers reject updates and deletes, so the journal preserves the decision history needed for review and compliance.

## How Codex was used

This project was built with Codex under clear product direction. You set the OpenAI-only stack, the GPT-5.6 requirement, the AR reconciliation domain, the append-only local audit trail, and the division between deterministic matching and model judgment.

Codex helped turn those requirements into a five-stage system. It designed the detailed boundary between code and model reasoning, used `Decimal` arithmetic for financial allocations, built the SSE flow, created synthetic data, and shaped the dashboard.

The work also included real verification. An early implementation followed patterns from an unrelated project and was deliberately discarded. The rebuilt version uses the standard OpenAI Python SDK only. Testing uncovered an unsafe auto-post path, an environment variable loading issue, confidence calibration drift, a false compliance match, and entity mapping gaps. Each problem was fixed with deterministic safeguards and regression tests.

The final audit script checks all 83 transactions across the ten synthetic datasets for badge consistency, routing safety, policy accuracy, and unsafe auto-posts.

## Data privacy

The model receives only the fields needed for its task. Entity resolution receives the raw payer, remittance, and a limited customer identity catalog. Routing receives a fixed allowlist of payment fields, the entity result, verified candidate facts, and deterministic amount facts.

Account numbers, routing numbers, tax IDs, arbitrary upstream fields, full ledger records, and full invoice records are not sent by default. A regression test confirms that injected sensitive fields stay out of the GPT-5.6 routing payload.

OpenAI API inputs and outputs are not used for model training by default. See [OpenAI API data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint) and [OpenAI business data privacy](https://openai.com/business-data/).

## Quick start

Prerequisites:

1. Python 3.11 or later
2. Node.js 18 or later
3. An OpenAI API key for live GPT-5.6 entity resolution and exception reasoning

Clone the repository and start the backend.

```bash
git clone https://github.com/vinaygangidi/ledger-sense.git
cd ledger-sense/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set OPENAI_API_KEY
uvicorn main:app --reload --port 8000
```

In a second terminal, start the frontend.

```bash
cd ledger-sense/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The synthetic demo also works without an API key for deterministic matching. Without a key, ambiguous cases safely route to human review. Set `OPENAI_API_KEY` to enable live GPT-5.6 judgment.

## Demo data

The repository contains ten synthetic bank-statement and open-AR datasets in `backend/data/samples/`. No real financial data is included.

The dashboard loads sample 04 by default and includes a dataset picker for all ten scenarios. Run a synthetic demo to see stage progress, compare incoming payments with the internal ledger, and inspect the final posting decisions.

The data covers 34 real-world cash application scenarios, including exact and multi-invoice matches, discounts, partial payments, parent and subsidiary relationships, factoring, identity aliases, duplicate payments, stale checks, compliance holds, disputes, and FX verification.

## Configuration

| Name | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | No | `""` | Enables GPT-5.6 entity resolution and exception routing. Without it both return safe review stubs and the deterministic pipeline still runs |
| `CORS_ORIGINS` | No | `http://localhost:3000,http://127.0.0.1:3000` | Comma-separated allowed browser origins |
| `NEXT_PUBLIC_API_URL` | No | `/api` | Frontend only. Browser-side backend base URL |
| `BACKEND_URL` | No | `http://localhost:8001` | Frontend rewrite target for `/api/:path*`. Prefixed with `https://` if no scheme is given |

`backend/.env` is loaded via `load_dotenv`, and a value already in the process
environment takes precedence over the file.

Note: `backend/Dockerfile` sets `ENV USE_FIXTURES=true`, but no code in this
repository reads `USE_FIXTURES`. It is a leftover and has no effect.

## Testing

```bash
cd backend
python -m unittest discover -s tests
```

Fourteen tests cover auto-post safety, entity resolution and canonical mapping,
deterministic matching strategies, policy gates, and FX/overpayment deltas. The
OpenAI client is mocked, so no test makes a network call.

## Limitations

- **Synthetic data only.** Ten datasets, 83 transactions, all generated by
  `backend/scripts/generate_samples.py`. No real bank statements or ledgers have been
  processed, and the edge-case mix reflects what the generator was written to produce.
- **SQLite audit journal is local and single-node.** The append-only guarantee comes from
  SQLite triggers on one file (`backend/data/audit.sqlite3`, gitignored). It is not
  replicated, backed up, or durable across container restarts, and it does not survive a
  redeploy. Real SOX-grade retention would need external storage.
- **The append-only guarantee is database-level, not tamper-proof.** Triggers block
  `UPDATE` and `DELETE` through SQL, but anyone with filesystem access can replace the
  database file. There is no signing or hash chaining.
- **No authentication.** Any caller who can reach the API can run a reconciliation and read
  audit records. `CORS_ORIGINS` restricts browsers; it is not an auth boundary.
- **`gpt-5.6` is hardcoded.** The model name is a string literal at two call sites in
  `reconciliation.py` and is not configurable by environment variable.
- **Test coverage is focused, not broad.** The 14 tests target the safety-critical paths —
  policy gates, allocation math, the payload allowlist. HTTP endpoints, the SSE stream, and
  the frontend have no tests, and there is no CI workflow.
- **No integration suite.** All tests mock the OpenAI client, so nothing verifies real model
  behavior, prompt drift, or API error handling against the live service.
- **Confidence thresholds are tuned, not validated.** The 0.95 auto-post threshold and the
  routing confidence bands were calibrated against the synthetic set. They have no
  statistical basis on real payment distributions.
- **Payer and remittance text still reaches OpenAI.** The field allowlist keeps account
  numbers, routing numbers, and tax IDs out of the routing payload, but raw payer names and
  remittance memos are sent for entity resolution. Those can contain customer-identifying
  information.
- **Single-process, in-request execution.** No queue or worker model; a reconciliation run
  occupies a request slot for its duration.

## License

MIT — see [LICENSE](LICENSE).
