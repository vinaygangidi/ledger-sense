"""OpenAI-only AR reconciliation: deterministic accounting, model-assisted routing."""
import asyncio, json, os, re, sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations
from pathlib import Path
from openai import AsyncOpenAI

CENT = Decimal("0.01")
STAGES = ["normalize", "ledger_index", "match", "exception_reasoning", "posting"]
def amount(value): return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
def name(value): return re.sub(r"\b(INC|LLC|LTD|CORP|CO|THE)\b", "", re.sub(r"[^A-Z0-9 ]", "", value.upper())).strip()

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
    response = await AsyncOpenAI().responses.create(model="gpt-5.6", input=json.dumps(prompt),
        text={"format":{"type":"json_object"}})
    try:
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
        return {"resolved_entity": result.get("resolved_entity"), "relationship": relationship,
                "confidence": confidence, "rationale": result.get("rationale", "No rationale returned.")}
    except Exception:
        return {"resolved_entity": None, "relationship": "unresolved", "confidence": .0,
                "rationale": "Entity-resolution response was not valid JSON."}

def candidates(payment, invoices):
    refs=payment["remittance"].upper()
    # A partially paid invoice remains eligible against its *remaining* open balance.
    # Its status describes payment history, not whether it can receive another payment.
    possible=[i for i in invoices if i["currency"]==payment["currency"] and i["status"] in {"OPEN","PARTIAL"}]
    choices=[]
    for inv in possible:
        ref=inv["invoice_id"] in refs or (inv.get("po_reference") and inv["po_reference"] in refs)
        same_name=inv["payer"]==payment["payer"]
        if (ref or same_name) and inv["open_amount"]==payment["amount"]:
            choices.append(("exact_reference" if ref else "exact_payer",[inv],.99))
        elif same_name and inv["open_amount"]>payment["amount"]:
            choices.append(("partial_payment",[inv],.82))
    payer_invoices=[i for i in possible if i["payer"]==payment["payer"]]
    for count in range(2,min(4,len(payer_invoices))+1):
        for group in combinations(payer_invoices,count):
            if sum((i["open_amount"] for i in group),Decimal("0"))==payment["amount"]:
                choices.append(("multi_invoice",list(group),.97))
    return sorted(choices,key=lambda c:c[2],reverse=True)

def enforce_auto_post_safety(decision, verified):
    """Auto-posting is permitted only with a high-confidence, code-verified allocation."""
    if decision["route"] == "auto_post" and (not verified or verified[0][2] < .95):
        return {
            "route": "review",
            "confidence": min(float(decision.get("confidence", .4)), .45),
            "reason": "Review required: no high-confidence deterministic invoice allocation was verified."
        }
    return decision

async def reason(payment, verified, entity):
    if not os.getenv("OPENAI_API_KEY"):
        return {"route":"review","confidence":.45,"reason":"No API key: safe demo review route."}
    prompt={"payment":{k:str(v) for k,v in payment.items()},
      "entity_resolution":entity,
      "candidates":[{"strategy":s,"invoice_ids":[i["invoice_id"] for i in group],"confidence":c} for s,group,c in verified],
      "instruction":"Return JSON with route (auto_post|review|dispute|compliance_hold), confidence (0..1), and rationale. Write one short analyst-readable rationale for this final route. Recommend auto_post only when a supplied candidate is a high-confidence, code-verified allocation. Do not invent amounts or invoice IDs."}
    response=await AsyncOpenAI().responses.create(model="gpt-5.6",input=json.dumps(prompt),
      text={"format":{"type":"json_object"}})
    try:
        result=parse_json(response.output_text)
        if not result: raise ValueError("empty JSON")
        return {"route":result.get("route","review"),"confidence":float(result.get("confidence",.4)),
                "reason":result.get("rationale","No analyst rationale returned.")}
    except Exception: return {"route":"review","confidence":.4,"reason":"Model output was not valid JSON."}

async def run_pipeline(run_id, bank, ledger, audit):
    def evt(stage,status,**extra): return {"event":"stage","run_id":run_id,"stage":stage,"status":status,**extra}
    yield evt("normalize","started")
    payments=[{**p,"amount":amount(p["amount"]),"currency":p.get("currency","USD"),"payer":name(p.get("payer_raw","")),"remittance":p.get("remittance_text","")} for p in bank.get("transactions",[])]
    await audit.append(run_id,"normalize","payments_normalized",{"count":len(payments)})
    yield evt("normalize","complete",count=len(payments))
    yield evt("ledger_index","started")
    invoices=[{**i,"open_amount":amount(i["open_amount"]),"currency":i.get("currency","USD"),"payer":name(i["customer_name"])} for i in ledger.get("invoices",[])]
    catalog=entity_catalog(ledger,invoices)
    await audit.append(run_id,"ledger_index","invoices_indexed",{"count":len(invoices)})
    yield evt("ledger_index","complete",count=len(invoices))
    postings=[]
    for payment in payments:
        entity=await resolve_entity(payment,catalog)
        if entity["resolved_entity"]:
            payment["payer"]=name(entity["resolved_entity"])
        await audit.append(run_id,"normalize","entity_resolved",{"txn_id":payment["txn_id"],**entity})
        yield evt("normalize","entity_resolved",transaction_id=payment["txn_id"],entity=entity)
        verified=candidates(payment,invoices)
        await audit.append(run_id,"match","candidates_verified",{"txn_id":payment["txn_id"],"count":len(verified)})
        yield evt("match","complete",transaction_id=payment["txn_id"],candidates=len(verified))
        decision=await reason(payment,verified,entity)
        hold_text = f"{payment.get('note', '')} {payment['remittance']}".upper()
        if re.search(r"\b(?:COMPLIANCE|SANCTIONS|LEGAL)\s+HOLD\b", hold_text):
            decision={"route":"compliance_hold","confidence":1,"reason":"Compliance hold: deterministic policy blocked posting."}
        elif verified and verified[0][2]>=.95:
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
    yield {"event":"complete","run_id":run_id,"results":postings}
