"""Portable report with historical trends, category breakdowns and trace drill-down."""
import html
import json
from pathlib import Path
from .core import load_run


def report(runs='runs', output='reports/index.html'):
    records=[]
    for path in sorted(Path(runs).glob('*/run.json')):
        try:
            meta,results=load_run(path.parent)
        except ValueError:
            continue
        records.append((meta,results))
    records.sort(key=lambda x:x[0]['created_at'])
    esc=lambda x:html.escape(str(x))
    parts=['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Agent evaluation report</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#182435}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px}summary{cursor:pointer;padding:10px}.pass{color:#087443}.fail{color:#b42318}a{color:#1463ad}meter{width:180px}details{border:1px solid #ddd;margin:10px 0;border-radius:5px}</style><h1>Continuous agent evaluation</h1><p>Live NVIDIA runs. Each case requires execution accuracy and a model-graded pass. Unknown API pricing is shown explicitly. Traces contain benchmark questions only.</p><h2>Pass rate over time</h2><table><tr><th>Run / UTC</th><th>Config</th><th>Pass rate</th><th>p50 / p95 agent latency</th><th>Tokens</th><th>Cost</th></tr>']
    for m,rs in records:
        s=m['summary'];cost='Unknown (rates not configured)' if s['cost_usd'] is None else f"${s['cost_usd']:.5f}"
        parts.append(f'<tr><td><a href="#{esc(m["id"])}">{esc(m["id"])}</a><br>{esc(m["created_at"])}</td><td>{esc(m["config"]["version"])}</td><td><meter min="0" max="1" value="{s["pass_rate"]}"></meter> {s["passed"]}/{s["total"]} ({s["pass_rate"]:.0%})</td><td>{s["agent_p50_seconds"]:.2f}s / {s["agent_p95_seconds"]:.2f}s</td><td>{s["total_tokens"]:,}</td><td>{cost}</td></tr>')
    parts.append('</table>')
    for m,rs in records:
        s=m['summary'];parts.append(f'<h2 id="{esc(m["id"])}">{esc(m["id"])}</h2><p>Config SHA: {esc(m["config_hash"][:16])} · Judge agreement: {s["judge_label_agreement"]:.0%} · Wall time: {m["wall_seconds"]:.1f}s</p>')
        diff=Path(runs)/m['id']/'comparison.json'
        if diff.exists():
            d=json.loads(diff.read_text());parts.append(f'<p class="{"pass" if d["passed"] else "fail"}">Gate: {"PASS" if d["passed"] else "FAIL"} — {esc("; ".join(d["reasons"]))}</p>')
        parts.append('<table><tr><th>Category</th><th>Passed / Total</th></tr>')
        for category,v in sorted(s['categories'].items()):parts.append(f'<tr><td>{esc(category)}</td><td>{v["passed"]}/{v["total"]}</td></tr>')
        parts.append('</table>')
        for r in rs:
            parts.append(f'<details><summary class="{"pass" if r["passed"] else "fail"}">{"PASS" if r["passed"] else "FAIL"} · {esc(r["case_id"])} · {esc(r["case"]["question"])}</summary><p>Execution: {r["execution_pass"]} · Judge: {r["judge_pass"]} · {esc(r.get("judge_reason",r.get("error")))}</p><pre>{esc(json.dumps(r,indent=2,ensure_ascii=False))}</pre></details>')
        parts.append('<details><summary>Authored-label judge calibration and confusion matrix</summary><pre>'+esc(json.dumps({'confusion':s['judge_confusion'],'labels':m['calibration']},indent=2))+'</pre></details>')
    parts.append('</html>');out=Path(output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text('\n'.join(parts));return out