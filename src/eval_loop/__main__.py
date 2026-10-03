import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from .core import compare, digest, load_run, read, run, validate_cases, write
from .report import report
from .sql import execute


def main():
    parser=argparse.ArgumentParser(description='Continuous NL-to-SQL evaluation loop')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('fetch-data');p.add_argument('--require-pin',action='store_true')
    p=sub.add_parser('run');p.add_argument('--config',default='configs/nemotron-v3.json');p.add_argument('--cases',default='evals/cases.json');p.add_argument('--out',default='runs');p.add_argument('--id')
    p=sub.add_parser('compare');p.add_argument('--baseline',default='runs/baseline-promoted');p.add_argument('--candidate',required=True)
    p=sub.add_parser('report');p.add_argument('--runs',default='runs');p.add_argument('--output',default='reports/index.html')
    p=sub.add_parser('validate');p.add_argument('--cases',default='evals/cases.json')
    p=sub.add_parser('capture');p.add_argument('--run',required=True);p.add_argument('--case',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('promote');p.add_argument('--draft',required=True);p.add_argument('--id',required=True);p.add_argument('--question',required=True);p.add_argument('--expected-sql');p.add_argument('--refusal',action='store_true');p.add_argument('--ordered',action='store_true');p.add_argument('--reviewer',required=True);p.add_argument('--cases',default='evals/cases.json')
    args=parser.parse_args()
    if args.command=='fetch-data':
        manifest=read('data/manifest.json');dest=Path('data/chinook.sqlite')
        data=dest.read_bytes() if dest.exists() else urllib.request.urlopen(manifest['url'],timeout=60).read()
        if hashlib.sha256(data).hexdigest()!=manifest['sha256']:raise ValueError('Database checksum mismatch')
        dest.write_bytes(data);print('Chinook checksum verified')
    elif args.command=='validate':
        cases=read(args.cases);validate_cases(cases,'data/chinook.sqlite');print(f'{len(cases)} valid cases')
    elif args.command=='run':
        metadata=run(args.config,cases_path=args.cases,out=args.out,run_id=args.id);print(json.dumps(metadata['summary'],indent=2))
    elif args.command=='compare':
        diff=compare(args.baseline,args.candidate);write(Path(args.candidate)/'comparison.json',diff);print(json.dumps(diff,indent=2));return 0 if diff['passed'] else 1
    elif args.command=='report':print(report(args.runs,args.output))
    elif args.command=='capture':
        meta,results=load_run(args.run);matches=[r for r in results if r['case_id']==args.case]
        if not matches:raise ValueError('Case not found')
        failure=matches[0]
        if failure['passed']:raise ValueError('Only failed cases can be captured')
        if Path(args.output).exists():raise ValueError('Draft already exists')
        write(args.output,{'status':'needs_review','source_run':meta['id'],'source_config_hash':meta['config_hash'],'failure':failure})
        print('Captured failure for review: '+args.output)
    elif args.command=='promote':
        draft=read(args.draft)
        if draft.get('status')!='needs_review':raise ValueError('Draft is not awaiting review')
        if args.refusal == bool(args.expected_sql):raise ValueError('Provide exactly one of --expected-sql or --refusal')
        if not args.reviewer.strip():raise ValueError('Reviewer required')
        cases=read(args.cases)
        if any(c['id']==args.id or c['question'].strip().casefold()==args.question.strip().casefold() for c in cases):
            raise ValueError('Duplicate case ID or question; add a distinct counterexample')
        case={'id':args.id,'category':draft['failure']['case']['category'],'difficulty':'hard','question':args.question,
              'expected_sql':args.expected_sql,'expected_refusal':args.refusal,'ordered':args.ordered,
              'rationale':'Regression from '+draft['source_run']+'/'+draft['failure']['case_id'],
              'provenance':{'source_run':draft['source_run'],'source_case':draft['failure']['case_id'],'reviewer':args.reviewer}}
        validate_cases(cases+[case],'data/chinook.sqlite')
        write(args.cases,cases+[case]);draft.update(status='promoted',promoted_id=args.id);write(args.draft,draft)
        print('Promoted '+args.id+' into '+args.cases+'; review diff and refresh baseline explicitly')
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr);sys.exit(2)