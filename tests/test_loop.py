import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from eval_loop.core import compare, digest, read, run, summarize, validate_cases, write
from eval_loop.provider import Client, ProviderError, JudgeResponseError, judge, parse_agent
from eval_loop.sql import equal_rows, execute, mutation_fixture

DB='data/chinook.sqlite'


class SqlTests(unittest.TestCase):
    def test_golden_cases_and_mutations(self):
        cases=read('evals/cases.json');self.assertIn(len(cases),(14,15))
        validate_cases(cases,DB)
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'mutation.sqlite';mutation_fixture(DB,db)
            for case in cases:
                if case['expected_sql']:execute(db,case['expected_sql'])
            ties=next(c for c in cases if c['id']=='top-ties')
            self.assertGreaterEqual(len(execute(db,ties['expected_sql'])),2)
            sales=next(c for c in cases if c['id']=='genre-sales')
            wrong=sales['expected_sql'].replace('il.UnitPrice*il.Quantity','t.UnitPrice')
            self.assertFalse(equal_rows(execute(db,wrong),execute(db,sales['expected_sql'])))

    def test_end_date_counterexample(self):
        case=next(c for c in read('evals/cases.json') if c['id']=='invoice-window')
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'dates.sqlite';mutation_fixture(DB,db,'mutations-v2')
            wrong="SELECT InvoiceId,Total FROM Invoice WHERE InvoiceDate >= '2010-01-01' AND InvoiceDate <= '2010-12-31' ORDER BY InvoiceId"
            self.assertFalse(equal_rows(execute(db,wrong),execute(db,case['expected_sql']),True))

    def test_read_only_and_resource_boundaries(self):
        before=execute(DB,'SELECT COUNT(*) FROM Track')
        for sql in ['DELETE FROM Track','DROP TABLE Track','PRAGMA user_version=4',"ATTACH DATABASE '/tmp/evil' AS evil",'SELECT 1; SELECT 2',"SELECT load_extension('evil')",'SELECT * FROM sqlite_master']:
            with self.subTest(sql=sql), self.assertRaises((sqlite3.Error,ValueError)):execute(DB,sql)
        self.assertEqual(before,execute(DB,'SELECT COUNT(*) FROM Track'))
        with self.assertRaises(ValueError):execute(DB,'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10002) SELECT x FROM n')
        with self.assertRaises(sqlite3.OperationalError):execute(DB,'WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n')

    def test_result_semantics(self):
        self.assertFalse(equal_rows([(1,),(1,)],[(1,)]))
        self.assertTrue(equal_rows([(1,),(2,)],[(2,),(1,)]))
        self.assertFalse(equal_rows([(1,),(2,)],[(2,),(1,)],True))
        self.assertFalse(equal_rows([(None,)],[(0,)]))
        self.assertFalse(equal_rows([('1',)],[(1,)]))
        self.assertTrue(equal_rows([(1.000000001,)],[(1,)]))
        self.assertTrue(equal_rows([],[]))

    def test_reject_duplicate_ids(self):
        case=read('evals/cases.json')[0]
        with self.assertRaises(ValueError):validate_cases([case,case],DB)


class ProviderTests(unittest.TestCase):
    def test_invalid_or_truncated_responses_fail(self):
        for response in ['{}','[]','{"sql":"DELETE FROM Track","refusal":true,"reason":"x"}','{"sql":null,"refusal":"false","reason":"x"}']:
            with self.assertRaises(ValueError):parse_agent({'content':response,'finish_reason':'stop'})
        with self.assertRaises(ValueError):parse_agent({'content':'{}','finish_reason':'length'})

    def test_malformed_judge_retains_usage_trace(self):
        trace={'content':'invalid JSON','finish_reason':'stop','prompt_tokens':12,'completion_tokens':3}
        client=SimpleNamespace(complete=lambda *a,**kw:trace)
        case=read('evals/cases.json')[0]
        with self.assertRaises(JudgeResponseError) as caught:
            judge(client,case,{'sql':'SELECT 1','refusal':False,'reason':''})
        self.assertEqual(caught.exception.trace['completion_tokens'],3)

    @patch.dict(os.environ,{'NVIDIA_API_KEY':'test-only-not-real'})
    def test_budget_prevents_request(self):
        config=read('configs/nemotron-v1.json');config['max_run_tokens']=1
        client=Client(config)
        with patch('urllib.request.urlopen') as request, self.assertRaises(ProviderError):client.complete([{'role':'user','content':'hello'}])
        request.assert_not_called()

    @patch.dict(os.environ,{'NVIDIA_API_KEY':'test-only-not-real'})
    def test_429_retry_and_usage(self):
        import io
        import urllib.error
        config=read('configs/nemotron-v1.json');client=Client(config)
        response={'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2}}
        error=urllib.error.HTTPError('https://example.invalid',429,'rate limit',{},None)
        with patch('urllib.request.urlopen',side_effect=[error,io.BytesIO(json.dumps(response).encode())]),patch('time.sleep'):
            trace=client.complete([{'role':'user','content':'hello'}])
        self.assertEqual(trace['attempts'],['http_429','ok']);self.assertEqual(trace['prompt_tokens'],10)
        self.assertIsNone(trace['cost_usd'])


class PipelineTests(unittest.TestCase):
    """Exercise real storage, execution grading, gates and CLI promotion offline."""
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cases=read('evals/cases.json');by_question={c['question']:c for c in cases}
        def complete(client,messages,judge=False):
            if judge:
                record=json.loads(messages[-1]['content'])
                labels=read('evals/judge_labels.json')
                matched=[x for x in labels if all(x[k]==record[k] for k in ('question','response','expected_sql','expected_refusal'))]
                content=json.dumps({'pass':matched[0]['label'] if matched else True,'reason':'Offline test double'})
            else:
                case=by_question[messages[-1]['content']]
                content=json.dumps({'sql':case['expected_sql'],'refusal':case['expected_refusal'],'reason':'policy' if case['expected_refusal'] else ''})
            return {'content':content,'finish_reason':'stop','latency_seconds':.01,'prompt_tokens':10,'completion_tokens':10,'cost_usd':None,'messages':messages}
        with patch.dict(os.environ,{'NVIDIA_API_KEY':'test-only-not-real'}),patch.object(Client,'complete',complete):
            run('configs/nemotron-v1.json',out=cls.root,run_id='base')
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def clone(self,name):
        import shutil
        dest=self.root/name
        if dest.exists():shutil.rmtree(dest)
        shutil.copytree(self.root/'base',dest)
        m=read(dest/'run.json');m['id']=name;write(dest/'run.json',m)
        return dest

    def test_identical_passes(self):
        dest=self.clone('identical');self.assertTrue(compare(self.root/'base',dest)['passed'])

    def test_regression_fails_and_promotes(self):
        dest=self.clone('bad');lines=[json.loads(l) for l in (dest/'results.jsonl').read_text().splitlines()]
        lines[0]['passed']=False;lines[0]['execution_pass']=False
        (dest/'results.jsonl').write_text(''.join(json.dumps(l)+'\n' for l in lines))
        meta=read(dest/'run.json');meta['summary']=summarize(lines,meta['calibration']);write(dest/'run.json',meta)
        comparison=compare(self.root/'base',dest);self.assertFalse(comparison['passed']);self.assertEqual(comparison['regressions'],[lines[0]['case_id']])
        env=dict(os.environ,PYTHONPATH='src')
        def cli(*args):return subprocess.run([sys.executable,'-m','eval_loop',*args],env=env,capture_output=True,text=True)
        self.assertEqual(cli('compare','--baseline',str(self.root/'base'),'--candidate',str(dest)).returncode,1)
        draft=self.root/'draft.json';suite=self.root/'promoted.json';write(suite,read('evals/cases.json'))
        self.assertEqual(cli('capture','--run',str(dest),'--case',lines[0]['case_id'],'--output',str(draft)).returncode,0)
        result=cli('promote','--draft',str(draft),'--id','regression-test','--question','Return the count of artists without albums.','--expected-sql','SELECT COUNT(*) FROM Artist a WHERE NOT EXISTS (SELECT 1 FROM Album al WHERE al.ArtistId=a.ArtistId)','--reviewer','unit-test','--cases',str(suite))
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(len(read(suite)),len(read('evals/cases.json'))+1)
        self.assertEqual(read(draft)['status'],'promoted')

    def test_latency_noise_floor_and_real_slowdown(self):
        dest=self.clone('latency')
        for seconds,expected in [(9,True),(11,False),(61,False)]:
            lines=[json.loads(l) for l in (dest/'results.jsonl').read_text().splitlines()]
            for r in lines:r['traces']['agent']['latency_seconds']=seconds
            (dest/'results.jsonl').write_text(''.join(json.dumps(l)+'\n' for l in lines))
            meta=read(dest/'run.json');meta['summary']=summarize(lines,meta['calibration']);write(dest/'run.json',meta)
            self.assertEqual(compare(self.root/'base',dest)['passed'],expected)

    def test_suite_mismatch_and_partial_run_fail_closed(self):
        dest=self.clone('mismatch');meta=read(dest/'run.json');meta['suite_hash']='different';write(dest/'run.json',meta)
        self.assertFalse(compare(self.root/'base',dest)['passed'])
        meta['status']='running';write(dest/'run.json',meta)
        with self.assertRaises(ValueError):compare(self.root/'base',dest)

    def test_report_escapes_model_output(self):
        from eval_loop.report import report
        dest=self.clone('xss');lines=[json.loads(l) for l in (dest/'results.jsonl').read_text().splitlines()];lines[0]['judge_reason']='<script>alert(1)</script>'
        (dest/'results.jsonl').write_text(''.join(json.dumps(l)+'\n' for l in lines))
        output=self.root/'report.html';report(self.root,output)
        self.assertNotIn('<script>alert(1)</script>',output.read_text());self.assertIn('&lt;script&gt;',output.read_text())


if __name__=='__main__':unittest.main()