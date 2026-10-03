import concurrent.futures
from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from .provider import Client, JudgeResponseError, judge, parse_agent
from .sql import equal_rows, execute, mutation_fixture, schema


def read(path):
    return json.loads(Path(path).read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    temporary.replace(path)


def percentile(values, p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else 0


def validate_cases(cases, db):
    if not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Eval set must be nonempty with unique IDs')
    for c in cases:
        if not isinstance(c['question'],str) or not c['question'].strip() or type(c['expected_refusal']) is not bool or type(c['ordered']) is not bool:
            raise ValueError('Invalid eval case: '+c['id'])
        if c['expected_refusal'] != (c['expected_sql'] is None):
            raise ValueError('Inconsistent refusal oracle: '+c['id'])
        if not c['expected_refusal']:
            execute(db,c['expected_sql'])


def verify_db(db):
    actual=hashlib.sha256(Path(db).read_bytes()).hexdigest()
    if actual != read('data/manifest.json')['sha256']:
        raise ValueError('Chinook checksum mismatch')
    return actual


def evaluate(case, client, prompt, databases):
    started=time.monotonic()
    result={'case_id':case['id'],'case_hash':digest(case),'case':case,'passed':False,'execution_pass':False,'judge_pass':False,'traces':{},'error':None}
    try:
        trace=client.complete([{'role':'system','content':prompt},{'role':'user','content':case['question']}])
        result['traces']['agent']=trace
        response=parse_agent(trace)
        result['response']=response
        checks=[]
        if case['expected_refusal']:
            result['execution_pass']=response['refusal'] and response['sql'] is None
        elif not response['refusal']:
            for name,db in databases:
                expected=execute(db,case['expected_sql'])
                actual=execute(db,response['sql'])
                ok=equal_rows(actual,expected,case['ordered'])
                checks.append({'fixture':name,'passed':ok,'expected_rows':len(expected),'actual_rows':len(actual),
                               'expected_preview':expected[:10],'actual_preview':actual[:10]})
            result['execution_pass']=all(c['passed'] for c in checks)
        result['execution_checks']=checks
        decision,trace=judge(client,case,response)
        result['traces']['judge']=trace
        result['judge_pass']=decision['pass']
        result['judge_reason']=decision['reason']
        result['passed']=result['execution_pass'] and result['judge_pass']
    except Exception as error:
        if isinstance(error,JudgeResponseError):
            result['traces']['judge']=error.trace
        # Exceptions here are local parsing/SQL failures or sanitized provider errors.
        result['error']=type(error).__name__+': '+str(error)[:300]
    result['latency_seconds']=round(time.monotonic()-started,4)
    return result


def calibrate(label,client):
    result={'id':label['id'],'label':label['label'],'prediction':None,'trace':None,'error':None}
    try:
        decision,trace=judge(client,label,label['response'])
        result.update(prediction=decision['pass'],trace=trace,reason=decision['reason'])
    except Exception as error:
        if isinstance(error,JudgeResponseError):result['trace']=error.trace
        result['error']=type(error).__name__+': '+str(error)[:300]
    return result


def summarize(results,calibration):
    traces=[t for r in results for t in r['traces'].values()]+[c['trace'] for c in calibration if c['trace']]
    safety=[r for r in results if r['case']['expected_refusal']]
    categories={}
    for r in results:
        bucket=categories.setdefault(r['case']['category'],{'passed':0,'total':0})
        bucket['total']+=1;bucket['passed']+=int(r['passed'])
    confusion={'tp':0,'tn':0,'fp':0,'fn':0,'errors':0}
    for c in calibration:
        if c['prediction'] is None: confusion['errors']+=1
        else: confusion[('t' if c['prediction']==c['label'] else 'f')+('p' if c['prediction'] else 'n')]+=1
    agent_times=[r['traces']['agent']['latency_seconds'] for r in results if 'agent' in r['traces']]
    costs=[t['cost_usd'] for t in traces]
    return dict(total=len(results),passed=sum(r['passed'] for r in results),pass_rate=sum(r['passed'] for r in results)/len(results),
        execution_pass_rate=sum(r['execution_pass'] for r in results)/len(results),
        refusal_pass_rate=sum(r['passed'] for r in safety)/len(safety) if safety else 0,
        categories=categories,judge_label_agreement=sum(c['prediction']==c['label'] for c in calibration)/len(calibration),
        judge_confusion=confusion,agent_p50_seconds=percentile(agent_times,.5),agent_p95_seconds=percentile(agent_times,.95),
        total_tokens=sum(t['prompt_tokens']+t['completion_tokens'] for t in traces),
        cost_usd=sum(costs) if costs and all(c is not None for c in costs) else None,
        errors=sum(r['error'] is not None for r in results),usage_note='Usage covers successful provider responses, including calibration; timed-out requests may be billed without returned usage.')


def run(config_path, *, cases_path='evals/cases.json', db='data/chinook.sqlite', out='runs', run_id=None):
    config=read(config_path);cases=read(cases_path);labels=read('evals/judge_labels.json')
    if len(cases)>config['max_cases']: raise ValueError('Case budget exceeded')
    if not 1<=config['concurrency']<=32: raise ValueError('Concurrency must be 1..32')
    db_hash=verify_db(db);validate_cases(cases,db)
    client=Client(config)
    now=dt.datetime.now(dt.timezone.utc)
    run_id=run_id or now.strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(3).hex()
    if not run_id or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for ch in run_id):
        raise ValueError('Invalid run ID')
    run_dir=Path(out)/run_id
    run_dir.mkdir(parents=True,exist_ok=False)
    metadata=dict(format_version=1,id=run_id,created_at=now.isoformat(),config=config,config_hash=digest(config),suite_hash=digest(cases),
                  labels_hash=digest(labels),database_hash=db_hash,fixture_version=config.get('fixture_version','mutations-v1'),status='running')
    write(run_dir/'run.json',metadata)
    started=time.monotonic();results=[]
    with tempfile.TemporaryDirectory() as tmp:
        mutant=Path(tmp)/'mutations.sqlite';mutation_fixture(db,mutant,metadata['fixture_version'])
        prompt=config['system_prompt'].replace('{schema}',schema(db))
        with concurrent.futures.ThreadPoolExecutor(max_workers=config['concurrency']) as pool:
            # Bounded submission: at most concurrency requests/futures in flight.
            iterator=iter(cases)
            pending={pool.submit(evaluate,c,client,prompt,[('chinook',db),(metadata['fixture_version'],mutant)]) for c in [next(iterator,None) for _ in range(config['concurrency'])] if c}
            with (run_dir/'results.jsonl').open('w') as stream:
                while pending:
                    done,pending=concurrent.futures.wait(pending,return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        result=future.result();results.append(result)
                        stream.write(json.dumps(result,ensure_ascii=False)+'\n');stream.flush()
                        print(f"{result['case_id']}: {'PASS' if result['passed'] else 'FAIL'}",flush=True)
                        c=next(iterator,None)
                        if c: pending.add(pool.submit(evaluate,c,client,prompt,[('chinook',db),(metadata['fixture_version'],mutant)]))
            calibration=list(pool.map(lambda c:calibrate(c,client),labels))
    results.sort(key=lambda r:r['case_id'])
    metadata.update(status='complete',wall_seconds=round(time.monotonic()-started,3),summary=summarize(results,calibration),calibration=calibration)
    write(run_dir/'run.json',metadata)
    index_run(out,metadata)
    return metadata


def index_run(out,metadata):
    with closing(sqlite3.connect(Path(out)/'index.sqlite')) as con, con:
        con.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT, config_hash TEXT, pass_rate REAL, summary TEXT)')
        con.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)',(metadata['id'],metadata['created_at'],metadata['config_hash'],metadata['summary']['pass_rate'],json.dumps(metadata['summary'])))


def load_run(path):
    path=Path(path);meta=read(path/'run.json')
    results=[json.loads(line) for line in (path/'results.jsonl').read_text().splitlines()]
    if meta.get('status')!='complete' or len(results)!=meta['summary']['total'] or len({r['case_id'] for r in results})!=len(results):
        raise ValueError('Incomplete or corrupt run')
    if digest(meta['config']) != meta['config_hash'] or any(digest(r['case']) != r['case_hash'] for r in results):
        raise ValueError('Run config or case hash mismatch')
    if summarize(results,meta['calibration']) != meta['summary']:
        raise ValueError('Run summary does not match recorded results')
    return meta,results


def compare(baseline,candidate,gates_path='configs/gates.json'):
    b,br=load_run(baseline);c,cr=load_run(candidate);g=read(gates_path)
    reasons=[]
    for k in ('suite_hash','labels_hash','database_hash','fixture_version'):
        if b[k]!=c[k]:reasons.append('Incompatible '+k+'; explicitly refresh baseline after reviewing suite changes')
    bi={r['case_id']:r for r in br};ci={r['case_id']:r for r in cr}
    missing=sorted(set(bi)-set(ci));added=sorted(set(ci)-set(bi))
    if missing:reasons.append('Missing baseline cases: '+', '.join(missing))
    regressions=[i for i in sorted(set(bi)&set(ci)) if bi[i]['passed'] and not ci[i]['passed']]
    improvements=[i for i in sorted(set(bi)&set(ci)) if not bi[i]['passed'] and ci[i]['passed']]
    bs,cs=b['summary'],c['summary']
    checks=[(cs['pass_rate']>=g['min_pass_rate'],'Pass rate below absolute floor'),
            (bs['pass_rate']-cs['pass_rate']<=g['max_pass_rate_drop']+1e-9,'Pass rate dropped'),
            (len(regressions)<=g['max_case_regressions'],'Paired case regression'),
            (cs['refusal_pass_rate']>=g['min_refusal_pass_rate'],'Safety refusal failed'),
            (cs['judge_label_agreement']>=g['min_judge_label_agreement'],'Judge calibration below floor'),
            (cs['errors']==0,'Run contains errors'),
            (cs['agent_p95_seconds']<=g['max_agent_p95_seconds'],'Agent p95 exceeds absolute ceiling'),
            (cs['agent_p95_seconds']<=max(g.get('min_latency_ratio_ceiling_seconds',0),bs['agent_p95_seconds']*g['max_latency_ratio']),'Agent p95 exceeds baseline ratio'),
            (cs['total_tokens']<=max(1,bs['total_tokens'])*g['max_total_tokens_ratio'],'Token use exceeds baseline ratio')]
    reasons.extend(reason for ok,reason in checks if not ok)
    return dict(baseline=b['id'],candidate=c['id'],passed=not reasons,reasons=reasons,regressions=regressions,improvements=improvements,
                missing=missing,added=added,pass_rate_delta=cs['pass_rate']-bs['pass_rate'],thresholds=g)