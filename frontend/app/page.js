"use client";
import {useEffect,useState} from "react";
const API=process.env.NEXT_PUBLIC_API_URL||"/api";
const stages=["normalize","ledger_index","match","exception_reasoning","posting"];
const label={normalize:"Normalize payments",ledger_index:"Index open AR",match:"Verify candidates",exception_reasoning:"Reason exceptions",posting:"Create postings"};
const money=(n,c="USD")=>new Intl.NumberFormat("en-US",{style:"currency",currency:c}).format(Number(n));
export default function Home(){
 const [data,setData]=useState(null),[done,setDone]=useState([]),[out,setOut]=useState([]),[run,setRun]=useState(""),[busy,setBusy]=useState(false);
 useEffect(()=>{fetch(`${API}/demo-data?sample=04`).then(x=>x.json()).then(setData)},[]);
 async function reconcile(){if(!data)return;setBusy(true);setDone([]);setOut([]);
  const r=await fetch(`${API}/analyze`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bank_data:data.bank_statement,ar_data:data.open_ar})});
  const reader=r.body.getReader();let buf="";while(true){const {value,done:closed}=await reader.read();if(closed)break;buf+=new TextDecoder().decode(value);const parts=buf.split("\n\n");buf=parts.pop();for(const p of parts){if(!p.startsWith("data: ")||p==="data: [DONE]")continue;const e=JSON.parse(p.slice(6));setRun(e.run_id||run);if(e.event==="stage")setDone(v=>v.includes(e.stage)?v:[...v,e.stage]);if(e.event==="stage"&&e.output)setOut(v=>[...v,e.output]);}}setBusy(false)}
 const payments=data?.bank_statement?.transactions||[];
 return <main>
  <header className="hero">
   <div><div className="hero-tags"><p className="eyebrow">OpenAI · GPT-5.6</p><span className="build-badge">Built for OpenAI Build Week — Codex + GPT-5.6</span></div><h1>AR Reconciliation<br/>Copilot</h1><p className="subtitle">Verified matching, with human-safe exception routing.</p></div>
   <button disabled={busy||!data} onClick={reconcile}><i>{busy?"◌":"↗"}</i>{busy?"Running pipeline…":"Run synthetic demo"}</button>
  </header>
  <section className="pipeline" aria-label="Reconciliation pipeline">{stages.map((s,i)=><div className={`stage ${done.includes(s)?"on":""}`} key={s}><b>{done.includes(s)?"✓":String(i+1).padStart(2,"0")}</b><div><strong>{label[s]}</strong><small>{s==="exception_reasoning"?"GPT-5.6 judgment":"Deterministic code"}</small></div></div>)}</section>
  <section className="comparison-head"><div><p className="section-kicker">Reconciliation workspace</p><h2>Payments, resolved.</h2></div><p>{busy?"Streaming agent decisions in real time.":"Run the sample pipeline to see each verified decision."}</p></section>
  <section className="columns">
   <article className="panel before"><div className="panel-head"><div><p className="panel-label">Input</p><h3>Bank payments</h3></div><em>{payments.length} transactions</em></div><div className="table-wrap"><table><thead><tr><th>ID</th><th>Payer</th><th>Remittance</th><th className="amount">Amount</th></tr></thead><tbody>{payments.map(p=><tr key={p.txn_id}><td className="mono">{p.txn_id}</td><td className="payer">{p.payer_raw}</td><td className="muted">{p.remittance_text||"—"}</td><td className="amount">{money(p.amount,p.currency)}</td></tr>)}</tbody></table></div></article>
   <article className="panel after"><div className="panel-head"><div><p className="panel-label">Verified output</p><h3>Posting instructions</h3></div><em>{out.length} decisions</em></div>{out.length?<div className="result-list">{out.map(p=><div className="result" key={p.transaction_id}><div className="result-top"><span className="mono">{p.transaction_id}</span><mark className={p.route}>{p.route.replaceAll("_"," ")}</mark></div><div className="decision"><div><p className="decision-label">Matched invoices</p><strong>{p.invoice_ids.join(", ")||"No verified invoice"}</strong></div><div className="confidence"><span>{Math.round(p.confidence*100)}%</span><small>confidence</small></div></div><div className="rationale"><span className="spark">✦</span><div><p>{p.reason}</p>{p.entity_resolution?.rationale&&<small>Entity resolution: {p.entity_resolution.rationale}</small>}</div></div></div>)}</div>:<div className="empty"><span>✦</span><strong>Your verified decisions will appear here.</strong><p>Each result includes an AI rationale, confidence score, and deterministic invoice match.</p></div>}</article>
  </section>
  <footer><span>All amounts are verified in code with Decimal.</span><span>GPT-5.6 never creates allocations.</span>{run&&<span>Audit run <code>{run}</code></span>}</footer>
 </main>
}
