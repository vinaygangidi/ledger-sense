import json
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reconciliation import AuditLog, run_pipeline

ROOT = Path(__file__).parent
audit = AuditLog(ROOT / "data" / "audit.sqlite3")
app = FastAPI(title="AR Reconciliation Copilot")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class AnalyzeRequest(BaseModel):
    bank_data: dict
    ar_data: dict

def sample_data(sample: str):
    folder = ROOT / "data" / "samples" / f"sample_{sample.zfill(2)}"
    return (json.loads((folder / "bank_statement.json").read_text()),
            json.loads((folder / "open_ar.json").read_text()))

@app.on_event("startup")
async def init_audit():
    await audit.initialize()

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ar-reconciliation-copilot",
            "model": "gpt-5.6", "audit": "append-only SQLite",
            "sample_count": len(list((ROOT / "data" / "samples").glob("sample_*")))}

@app.get("/samples")
async def samples():
    return {"samples": [json.loads((p / "meta.json").read_text()) for p in sorted((ROOT / "data" / "samples").glob("sample_*"))]}

@app.get("/demo-data")
async def demo_data(sample: str = "01"):
    bank, ledger = sample_data(sample)
    return {"bank_statement": bank, "open_ar": ledger, "sample_id": sample}

@app.get("/audit/{run_id}")
async def audit_events(run_id: str):
    return {"events": await audit.events(run_id)}

@app.post("/analyze")
async def analyze(request: AnalyzeRequest):
    run_id = str(uuid.uuid4())
    async def stream():
        async for event in run_pipeline(run_id, request.bank_data, request.ar_data, audit):
            yield f"data: {json.dumps(event, default=str)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
