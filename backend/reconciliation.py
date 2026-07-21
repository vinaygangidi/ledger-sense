"""OpenAI-only AR reconciliation: deterministic accounting, model-assisted routing."""
import asyncio, json, logging, os, re, sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations
from pathlib import Path
from openai import AsyncOpenAI

CENT = Decimal("0.01")
MODEL_TIMEOUT_SECONDS = 20.0
STAGES = ["normalize", "ledger_index", "match", "exception_reasoning", "posting"]
ROUTING_PAYMENT_FIELDS = (
    "txn_id", "payer_raw", "remittance_text", "amount", "currency",
    "payment_type", "note", "statement_date", "payer", "remittance",
)
logger = logging.getLogger("uvicorn.error")
def amount(value): return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
def name(value): return re.sub(r"\b(INC|LLC|LTD|CORP|CO|THE)\b", "", re.sub(r"[^A-Z0-9 ]", "", value.upper())).strip()


def masked_payer(payment):
    """Keep demo logs useful without recording a full payer name."""
    payer = re.sub(r"\s+", " ", payment.get("payer_raw", "").strip())
    return f"{payer[:3]}… ({len(payer)} chars)" if payer else "<missing>"

class AuditLog:
    def __init__(self, path: Path): self.path = path
    async def initialize(self):
        def create():
            self.path.parent.mkdir(exist_ok=True)
            with sqlite3.connect(self.path) as db:
                db.executescript("""CREATE TABLE IF NOT EXISTS audit_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
                  stage TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL);
                  CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
                    BEGIN SELECT RAISE(ABORT,'append-only audit'); END;
                  CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
                    BEGIN SELECT RAISE(ABORT,'append-only audit'); END;""")
        await asyncio.to_thread(create)
    async def append(self, run_id, stage, action, payload):
        def insert():
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO audit_events(run_id,created_at,stage,action,payload) VALUES(?,?,?,?,?)",
                    (run_id, datetime.now(timezone.utc).isoformat(), stage, action, json.dumps(payload, default=str)))
        await asyncio.to_thread(insert)
    async def events(self, run_id):
        def read():
            with sqlite3.connect(self.path) as db:
                rows=db.execute("SELECT id,created_at,stage,action,payload FROM audit_events WHERE run_id=? ORDER BY id",(run_id,)).fetchall()
            return [{"id":r[0],"created_at":r[1],"stage":r[2],"action":r[3],"payload":json.loads(r[4])} for r in rows]
        return await asyncio.to_thread(read)

def entity_catalog(ledger, invoices):
    customers = {}
    for invoice in invoices:
        customers.setdefault(invoice["customer_name"], {"customer_name": invoice["customer_name"], "customer_id": invoice.get("customer_id"), "aliases": [], "relationships": [], "ledger_notes": []})
        customers[invoice["customer_name"]]["ledger_notes"].append(invoice.get("note", ""))
    by_id = {c["customer_id"]: c for c in customers.values()}
    for alias in ledger.get("payer_alias_registry", []):
        customer = by_id.get(alias.get("canonical_customer_id"))
        if customer:
            customer["aliases"].append(alias.get("payer_alias"))
            customer["relationships"].append(alias.get("match_type", "ALIAS"))
    for parent in ledger.get("parent_child_hierarchy", []):
        for child_id in parent.get("children", []):
            customer = by_id.get(child_id)
            if customer:
                customer["aliases"].append(parent.get("parent_name"))
                customer["relationships"].append("PARENT_PAYS_CHILD")
    return list(customers.values())

def parse_json(text):
    try: return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text or "")
        return json.loads(match.group(0)) if match else None

async def resolve_entity(payment, catalog):
    """GPT-5.6 judges entity identity; it is restricted to ledger-supplied candidates."""
    if not os.getenv("OPENAI_API_KEY"):
        return {"resolved_entity": None, "relationship": "unresolved", "confidence": .0,
                "rationale": "No API key: entity identity left unresolved for safe deterministic demo."}
    prompt = {
        "raw_payer": payment["payer_raw"], "remittance": payment["remittance"],
        "known_entities": catalog,
        "task": "Resolve the payer to exactly one supplied entity only when evidence supports it. Consider truncation, DBA/alias, parent/subsidiary, and factoring intermediary relationships. Return JSON: resolved_entity (or null), relationship (direct|dba_alias|parent_paying|factoring_intermediary|unresolved), confidence (0..1), rationale (one short analyst-readable sentence). Calibrate confidence: 0.90-1.00 only for exact or documented ledger evidence; 0.60-0.89 for plausible but incomplete evidence; 0.00-0.35 when unresolved, generic, or weak. Never invent an entity."
    }
    try:
        logger.info("Calling GPT-5.6 for entity resolution: transaction_id=%s payer=%s", payment.get("txn_id", "unknown"), masked_payer(payment))
        response = await AsyncOpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=0).responses.create(
            model="gpt-5.6", input=json.dumps(prompt), text={"format":{"type":"json_object"}}
        )
        result = parse_json(response.output_text)
        if not result: raise ValueError("empty JSON")
        allowed = {c["customer_name"] for c in catalog}
        by_id = {c["customer_id"]: c["customer_name"] for c in catalog}
        entity = result.get("resolved_entity")
        if isinstance(entity, dict):
            entity = entity.get("customer_id") or entity.get("customer_name")
        if entity in by_id:
            result["resolved_entity"] = by_id[entity]
        elif entity in allowed:
            result["resolved_entity"] = entity
        else:
            result["resolved_entity"] = None
        relationship = result.get("relationship", "unresolved")
        confidence = float(result.get("confidence", 0))
        if result.get("resolved_entity") is None or relationship == "unresolved":
            confidence = min(confidence, .35)
        logger.info("GPT-5.6 entity response: transaction_id=%s relationship=%s confidence=%d%%", payment.get("txn_id", "unknown"), relationship, round(confidence * 100))
        return {"resolved_entity": result.get("resolved_entity"), "relationship": relationship,
                "confidence": confidence, "rationale": result.get("rationale", "No rationale returned.")}
    except Exception:
        return {"resolved_entity": None, "relationship": "unresolved", "confidence": .0,
                "rationale": "Entity-resolution response was not valid JSON."}

def has_text(payment, *phrases):
    text = f"{payment.get('note', '')} {payment.get('remittance', '')}".upper()
    return any(phrase in text for phrase in phrases)

def referenced(invoice, refs):
    legacy = re.findall(r"(?:LEGACY-INV|SAP)-[A-Z0-9-]+", invoice.get("note", "").upper())
    return (
        invoice["invoice_id"] in refs
        or (invoice.get("po_reference") and invoice["po_reference"] in refs)
        or any(value in refs for value in legacy)
    )

def fx_verified(payment, invoice):
    match = re.search(r"\b(?:EUR|GBP|CAD)\s+([0-9]+(?:\.[0-9]+)?)\s+@\s+([0-9]+(?:\.[0-9]+)?)", payment["remittance"].upper())
    if not match:
        return False
    foreign, rate = amount(match.group(1)), Decimal(match.group(2))
    return (foreign * rate).quantize(CENT, rounding=ROUND_HALF_UP) == payment["amount"] == invoice["open_amount"]

def amount_facts(payment, invoices):
    """Expose deterministic arithmetic to the model without asking it to calculate."""
    facts=[]
    refs=payment["remittance"].upper()
    for invoice in invoices:
        if invoice["currency"] != payment["currency"] or not (invoice["payer"] == payment["payer"] or referenced(invoice, refs)):
            continue
        delta=(payment["amount"]-invoice["open_amount"]).quantize(CENT)
        if delta > 0:
            facts.append({"invoice_id":invoice["invoice_id"],"kind":"overpayment","delta":str(delta)})
        elif delta < 0:
            facts.append({"invoice_id":invoice["invoice_id"],"kind":"short_payment","delta":str(-delta)})
        fx=re.search(r"\b(?:EUR|GBP|CAD)\s+([0-9]+(?:\.[0-9]+)?)\s+@\s+([0-9]+(?:\.[0-9]+)?)", payment["remittance"].upper())
        if fx:
            expected=(amount(fx.group(1))*Decimal(fx.group(2))).quantize(CENT, rounding=ROUND_HALF_UP)
            facts.append({"invoice_id":invoice["invoice_id"],"kind":"fx_conversion","expected_amount":str(expected),"actual_amount":str(payment["amount"]),"delta":str((payment["amount"]-expected).quantize(CENT))})
    return facts

def candidates(payment, invoices, ledger=None):
    """Return only amount-verified allocations; exceptions are routed by deterministic policy."""
    refs=payment["remittance"].upper()
    config=(ledger or {}).get("auto_writeoff_config", {})
    writeoff=amount(config.get("threshold", 0))
    # A partially paid invoice remains eligible against its *remaining* open balance.
    possible=[i for i in invoices if i["currency"]==payment["currency"] and i["status"] in {"OPEN","PARTIAL"}]
    choices=[]
    credits=(ledger or {}).get("credit_memos", [])
    for inv in possible:
        ref=referenced(inv, refs)
        same_name=inv["payer"]==payment["payer"]
        if not (ref or same_name):
            continue
        if payment["amount"] == inv["open_amount"]:
            if not refs and same_name:
                continue
            strategy = "fx_verified" if fx_verified(payment, inv) else ("exact_reference" if ref else "amount_match")
            confidence = .98 if strategy == "fx_verified" else (.90 if strategy == "amount_match" else .99)
            choices.append((strategy,[inv],confidence))
        elif same_name and inv["open_amount"] > payment["amount"]:
            discount = Decimal(str(inv.get("discount_pct", 0))) / Decimal("100")
            deadline = inv.get("discount_deadline")
            paid_on = payment.get("date")
            discounted=(inv["open_amount"] * (Decimal("1") - discount)).quantize(CENT, rounding=ROUND_HALF_UP)
            if discount and deadline and paid_on and paid_on <= deadline and payment["amount"] == discounted:
                choices.append(("early_pay_discount",[inv],.96))
            else:
                credit_total=sum((amount(c["amount"]) for c in credits if c.get("applied_to_invoice")==inv["invoice_id"]), Decimal("0"))
                if credit_total and payment["amount"] + credit_total == inv["open_amount"]:
                    choices.append(("credit_memo_net",[inv],.97))
                elif inv["open_amount"] - payment["amount"] <= writeoff and has_text(payment, "WIRE FEE", "BANK FEE"):
                    choices.append(("wire_fee_writeoff",[inv],.96))
                else:
                    choices.append(("partial_payment",[inv],.82))
    payer_invoices=[i for i in possible if i["payer"]==payment["payer"]]
    if not refs:
        fifo=sorted((i for i in payer_invoices if i["open_amount"]==payment["amount"]), key=lambda i:i.get("invoice_date",""))
        if fifo:
            choices.append(("fifo_amount_match",[fifo[0]],.96))
    for count in range(2,min(4,len(payer_invoices))+1):
        for group in combinations(payer_invoices,count):
            if sum((i["open_amount"] for i in group),Decimal("0"))==payment["amount"]:
                choices.append(("multi_invoice",list(group),.97))
    return sorted(choices,key=lambda c:c[2],reverse=True)

def deterministic_policy(payment, invoices, ledger, seen_payments):
    """Block unsafe timing, compliance, and exception cases before any model recommendation."""
    text=f"{payment.get('note', '')} {payment.get('remittance', '')}".upper()
    refs=[i for i in invoices if i["invoice_id"] in payment["remittance"].upper()]
    route=None
    payment_date=date.fromisoformat(payment["date"]) if payment.get("date") else None
    statement_date=date.fromisoformat(payment["statement_date"]) if payment.get("statement_date") else None
    if payment.get("payment_type") == "CHECK" and payment_date and statement_date and payment_date > statement_date:
        route=("review", "Deterministic policy: post-dated check cannot be applied before its check date.")
    elif payment.get("payment_type") == "CHECK" and payment_date and statement_date and (statement_date-payment_date).days > 180:
        route=("review", "Deterministic policy: stale check exceeds the 180-day negotiation limit.")
    elif re.search(r"\b(?:COMPLIANCE|SANCTIONS|OFAC|LEGAL)\s+HOLD\b", text):
        route=("compliance_hold", "Deterministic policy: compliance or legal hold blocked posting.")
    elif "DISPUTED" in text or any(i.get("status")=="DISPUTED" or i.get("dispute_reason") for i in refs):
        route=("dispute", "Deterministic policy: disputed invoice requires credit-team review.")
    elif any(marker in text for marker in ("DUPLICATE PAYMENT", "NSF RETURN", "POST-DATED", "STALE CHECK")):
        route=("review", "Deterministic policy: documented exception requires analyst review.")
    key=(payment["payer"], payment["amount"], payment["remittance"])
    if key in seen_payments:
        route=("review", "Deterministic policy: duplicate payer, amount, and remittance detected in this batch.")
    seen_payments.add(key)
    if route:
        return {"route":route[0],"confidence":1,"reason":route[1]}
    return None

def enforce_auto_post_safety(decision, verified):
    """Auto-posting is permitted only with a high-confidence, code-verified allocation."""
    if decision["route"] == "auto_post" and (not verified or verified[0][2] < .95):
        return {
            "route": "review",
            "confidence": min(float(decision.get("confidence", .4)), .45),
            "reason": "Review required: no high-confidence deterministic invoice allocation was verified."
        }
    return decision

async def reason(payment, verified, entity, facts=None):
    if not os.getenv("OPENAI_API_KEY"):
        return {"route":"review","confidence":.45,"reason":"No API key: safe demo review route."}
    # Never pass arbitrary upstream payment keys to the model. New integration
    # fields (for example account_number, routing_number, or tax_id) stay local
    # unless deliberately added to this reviewed allowlist.
    model_payment = {field: str(payment[field]) for field in ROUTING_PAYMENT_FIELDS if field in payment}
    prompt={"payment":model_payment,
      "entity_resolution":entity,
      "deterministic_amount_facts":facts or [],
      "candidates":[{"strategy":s,"invoice_ids":[i["invoice_id"] for i in group],"confidence":c} for s,group,c in verified],
      "instruction":"Return JSON with route (auto_post|review|dispute|compliance_hold), confidence (0..1), and rationale. Write one short analyst-readable rationale for this final route. Recommend auto_post only when a supplied candidate is a high-confidence, code-verified allocation. When routing to review because the highest supplied candidate is below 0.95 confidence, the rationale MUST name that candidate's strategy and its exact numeric confidence formatted to two decimals (for example, 'partial_payment at 0.82 confidence'); do not use only vague wording such as 'not high-confidence'. Do not invent amounts or invoice IDs."}
    try:
        logger.info("Calling GPT-5.6 for routing: transaction_id=%s verified_candidates=%d", payment.get("txn_id", "unknown"), len(verified))
        response=await AsyncOpenAI(timeout=MODEL_TIMEOUT_SECONDS, max_retries=0).responses.create(
          model="gpt-5.6",input=json.dumps(prompt),text={"format":{"type":"json_object"}}
        )
        result=parse_json(response.output_text)
        if not result: raise ValueError("empty JSON")
        route=result.get("route","review")
        confidence=float(result.get("confidence",.4))
        logger.info("GPT-5.6 routing response: transaction_id=%s route=%s confidence=%d%%", payment.get("txn_id", "unknown"), route, round(confidence * 100))
        return {"route":route,"confidence":confidence,
                "reason":result.get("rationale","No analyst rationale returned.")}
    except Exception: return {"route":"review","confidence":.4,"reason":"Model output was not valid JSON."}

async def run_pipeline(run_id, bank, ledger, audit):
    def evt(stage,status,**extra): return {"event":"stage","run_id":run_id,"stage":stage,"status":status,**extra}
    yield evt("normalize","started")
    payments=[{**p,"statement_date":bank.get("statement_date"),"amount":amount(p["amount"]),"currency":p.get("currency","USD"),"payer":name(p.get("payer_raw","")),"remittance":p.get("remittance_text","")} for p in bank.get("transactions",[])]
    await audit.append(run_id,"normalize","payments_normalized",{"count":len(payments)})
    yield evt("normalize","complete",count=len(payments))
    yield evt("ledger_index","started")
    invoices=[{**i,"open_amount":amount(i["open_amount"]),"currency":i.get("currency","USD"),"payer":name(i["customer_name"])} for i in ledger.get("invoices",[])]
    catalog=entity_catalog(ledger,invoices)
    await audit.append(run_id,"ledger_index","invoices_indexed",{"count":len(invoices)})
    yield evt("ledger_index","complete",count=len(invoices))
    postings=[]
    seen_payments=set()
    for payment in payments:
        entity=await resolve_entity(payment,catalog)
        if entity["resolved_entity"]:
            payment["payer"]=name(entity["resolved_entity"])
        await audit.append(run_id,"normalize","entity_resolved",{"txn_id":payment["txn_id"],**entity})
        yield evt("normalize","entity_resolved",transaction_id=payment["txn_id"],entity=entity)
        verified=candidates(payment,invoices,ledger)
        await audit.append(run_id,"match","candidates_verified",{"txn_id":payment["txn_id"],"count":len(verified)})
        yield evt("match","complete",transaction_id=payment["txn_id"],candidates=len(verified))
        facts=amount_facts(payment,invoices)
        decision=await reason(payment,verified,entity,facts)
        policy=deterministic_policy(payment,invoices,ledger,seen_payments)
        if policy:
            decision=policy
        elif verified and verified[0][2]>=.95 and decision["route"]=="auto_post":
            source=" via GPT-5.6 entity resolution" if entity["resolved_entity"] else ""
            fallback=f"Auto-posted: verified {verified[0][0]} allocation{source}."
            decision={"route":"auto_post","confidence":verified[0][2],
                      "reason":decision["reason"] if os.getenv("OPENAI_API_KEY") else fallback}
        decision=enforce_auto_post_safety(decision, verified)
        await audit.append(run_id,"exception_reasoning","route_decided",{"txn_id":payment["txn_id"],**decision})
        match=verified[0] if decision["route"]=="auto_post" and verified else None
        posting={"transaction_id":payment["txn_id"],"route":decision["route"],"confidence":decision["confidence"],
          "reason":decision["reason"],"entity_resolution":entity,"amount":str(payment["amount"]),"currency":payment["currency"],
          "invoice_ids":[i["invoice_id"] for i in match[1]] if match else [],
          "allocation_verified":bool(match and sum((i["open_amount"] for i in match[1]),Decimal("0"))==payment["amount"])}
        await audit.append(run_id,"posting","posting_instruction",posting); postings.append(posting)
        yield evt("posting","complete",transaction_id=payment["txn_id"],output=posting)
    # The dashboard marks stage 4 complete only once every routing decision is final.
    yield evt("exception_reasoning","complete",count=len(postings))
    yield {"event":"complete","run_id":run_id,"results":postings}
