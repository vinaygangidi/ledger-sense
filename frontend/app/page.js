"use client";
import {useEffect,useState} from "react";
const API=process.env.NEXT_PUBLIC_API_URL||"/api";
const stages=["normalize","ledger_index","match","exception_reasoning","posting"];
const label={normalize:"Normalize payments",ledger_index:"Index open AR",match:"Verify candidates",exception_reasoning:"Reason exceptions",posting:"Create postings"};
const money=(n,c="USD")=>new Intl.NumberFormat("en-US",{style:"currency",currency:c}).format(Number(n));
export default function Home(){
 const [data,setData]=useState(null),[done,setDone]=useState([]),[out,setOut]=useState([]),[run,setRun]=useState(""),[busy,setBusy]=useState(false);
 useEffect(()=>{fetch(`${API}/demo-data?sample=01`).then(x=>x.json()).then(setData)},[]);
 async function reconcile(){if(!data)return;setBusy(true);setDone([]);setOut([]);
  const r=await fetch(`${API}/analyze`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bank_data:data.bank_statement,ar_data:data.open_ar})});
  const reader=r.body.getReader();let buf="";while(true){const {value,done:closed}=await reader.read();if(closed)break;buf+=new TextDecoder().decode(value);const parts=buf.split("\n\n");buf=parts.pop();for(const p of parts){if(!p.startsWith("data: ")||p==="data: [DONE]")continue;const e=JSON.parse(p.slice(6));setRun(e.run_id||run);if(e.event==="stage")setDone(v=>v.includes(e.stage)?v:[...v,e.stage]);if(e.event==="stage"&&e.output)setOut(v=>[...v,e.output]);}}setBusy(false)}
 const payments=data?.bank_statement?.transactions||[];
 return <main><header><p>OPENAI · GPT-5.6</p><h1>AR Reconciliation Copilot</h1><span>Verified matching. Human-safe exception routing.</span><button disabled={busy||!data} onClick={reconcile}>{busy?"Running pipeline…":"Run synthetic demo"}</button></header>
 <section className="pipeline">{stages.map((s,i)=><div className={done.includes(s)?"on":""} key={s}><b>{i+1}</b><strong>{label[s]}</strong><small>{s==="exception_reasoning"?"GPT-5.6":"deterministic code"}</small></div>)}</section>
 <section className="columns"><article><h2>Before: bank payments <em>{payments.length}</em></h2><table><thead><tr><th>ID</th><th>Payer</th><th>Remittance</th><th>Amount</th></tr></thead><tbody>{payments.map(p=><tr key={p.txn_id}><td>{p.txn_id}</td><td>{p.payer_raw}</td><td>{p.remittance_text||"—"}</td><td>{money(p.amount,p.currency)}</td></tr>)}</tbody></table></article>
 <article><h2>After: posting instructions <em>{out.length}</em></h2>{out.length?<table><thead><tr><th>ID</th><th>Route</th><th>Invoices</th><th>Confidence</th></tr></thead><tbody>{out.map(p=><tr key={p.transaction_id}><td>{p.transaction_id}</td><td><mark className={p.route}>{p.route}</mark></td><td>{p.invoice_ids.join(", ")||"—"}</td><td>{Math.round(p.confidence*100)}%</td></tr>)}</tbody></table>:<div className="empty">Run the demo to stream verifiable posting decisions.</div>}</article></section>
 <footer>All amounts are checked in code with Decimal. GPT-5.6 never creates allocations. {run&&<>Audit run <code>{run}</code></>}</footer></main>
}
