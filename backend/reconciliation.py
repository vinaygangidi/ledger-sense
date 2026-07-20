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

def candidates(payment, invoices):
    refs=payment["remittance"].upper()
    possible=[i for i in invoices if i["currency"]==payment["currency"] and i["status"]=="OPEN"]
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

async def reason(payment, verified):
    if not os.getenv("OPENAI_API_KEY"):
        return {"route":"review","confidence":.45,"reason":"No API key: safe demo review route."}
    prompt={"payment":{k:str(v) for k,v in payment.items()},
      "candidates":[{"strategy":s,"invoice_ids":[i["invoice_id"] for i in group],"confidence":c} for s,group,c in verified],
      "instruction":"Return JSON with route (auto_post|review|dispute|compliance_hold), confidence (0..1), and reason. Do not invent amounts or invoice IDs."}
    response=await AsyncOpenAI().responses.create(model="gpt-5.6",input=json.dumps(prompt),
      text={"format":{"type":"json_object"}})
    try: return json.loads(response.output_text)
    except Exception: return {"route":"review","confidence":.4,"reason":"Model output was not valid JSON."}

async def run_pipeline(run_id, bank, ledger, audit):
    def evt(stage,status,**extra): return {"event":"stage","run_id":run_id,"stage":stage,"status":status,**extra}
    yield evt("normalize","started")
    payments=[{**p,"amount":amount(p["amount"]),"currency":p.get("currency","USD"),"payer":name(p.get("payer_raw","")),"remittance":p.get("remittance_text","")} for p in bank.get("transactions",[])]
    await audit.append(run_id,"normalize","payments_normalized",{"count":len(payments)})
    yield evt("normalize","complete",count=len(payments))
    yield evt("ledger_index","started")
    invoices=[{**i,"open_amount":amount(i["open_amount"]),"currency":i.get("currency","USD"),"payer":name(i["customer_name"])} for i in ledger.get("invoices",[])]
    await audit.append(run_id,"ledger_index","invoices_indexed",{"count":len(invoices)})
    yield evt("ledger_index","complete",count=len(invoices))
    postings=[]
    for payment in payments:
        verified=candidates(payment,invoices)
        await audit.append(run_id,"match","candidates_verified",{"txn_id":payment["txn_id"],"count":len(verified)})
        yield evt("match","complete",transaction_id=payment["txn_id"],candidates=len(verified))
        decision=await reason(payment,verified)
        if "HOLD" in (payment.get("note","")+payment["remittance"]).upper(): decision={"route":"compliance_hold","confidence":1,"reason":"Deterministic compliance policy."}
        elif verified and verified[0][2]>=.95: decision={"route":"auto_post","confidence":verified[0][2],"reason":"Verified exact Decimal allocation."}
        await audit.append(run_id,"exception_reasoning","route_decided",{"txn_id":payment["txn_id"],**decision})
        match=verified[0] if decision["route"]=="auto_post" and verified else None
        posting={"transaction_id":payment["txn_id"],"route":decision["route"],"confidence":decision["confidence"],
          "reason":decision["reason"],"amount":str(payment["amount"]),"currency":payment["currency"],
          "invoice_ids":[i["invoice_id"] for i in match[1]] if match else [],
          "allocation_verified":bool(match and sum((i["open_amount"] for i in match[1]),Decimal("0"))==payment["amount"])}
        await audit.append(run_id,"posting","posting_instruction",posting); postings.append(posting)
        yield evt("posting","complete",transaction_id=payment["txn_id"],output=posting)
    yield {"event":"complete","run_id":run_id,"results":postings}
