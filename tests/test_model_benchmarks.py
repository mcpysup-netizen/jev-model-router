import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_benchmarks as b
import jev_model_router as r


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / 'cache.json'
        self.table = Path(self.tmp.name) / 'table.md'
        self.models = {
            'codex_sol': dict(provider='codex', model='gpt-6.1-sol', capability=2, model_facts={}, efforts=('medium','high','xhigh','max')),
            'claude_sonnet': dict(provider='claude', model='sonnet', capability=2, model_facts={'api_model_id':'claude-sonnet-5-5'}, efforts=('medium','high','xhigh','max')),
            'claude_haiku': dict(provider='claude', model='haiku', capability=1, model_facts={}, efforts=()),
        }
        self.benchmark = dict(name='LiveBench release Coding', score=90, unit='%', effort='max',
                              kinds=['code','review'], comparison_group='same', comparable=True)

    def test_partial_scores_restore_legacy_for_three_kinds_under_five(self):
        self.models['claude_sonnet']['official_performance'] = {'benchmarks':[self.benchmark]}
        rows = [dict(model='gpt-6.1-sol',kind='code',risk='medium',scope='small',verdict='pass',cost_usd=None,minutes=None,rework=None)] * 4
        for kind, risk, expected in [('code','medium','codex_sol'), ('review','medium','codex_sol'), ('mechanical','low','claude_haiku')]:
            self.assertEqual(r.evidence_fallback(kind,risk,'small',self.models,rows), (expected,'legacy_rules'))
            self.assertIn('비교 불가 사유',self.models[expected]['comparison_unavailable_reason'])

    def test_full_coverage_and_conditions_are_required(self):
        for item in self.models.values():
            item['official_performance']={'benchmarks':[dict(self.benchmark)]}
        self.models['codex_sol']['official_performance']['benchmarks'][0]['score']=91
        self.assertEqual(r.evidence_fallback('code','medium','small',self.models,[])[1],'official_benchmark')
        for field, value in [('effort','high'),('comparison_group','other'),('name','different'),('comparable',False)]:
            old=self.models['codex_sol']['official_performance']['benchmarks'][0][field]
            self.models['codex_sol']['official_performance']['benchmarks'][0][field]=value
            self.assertEqual(r.evidence_fallback('code','medium','small',self.models,[])[1],'legacy_rules')
            self.models['codex_sol']['official_performance']['benchmarks'][0][field]=old

    def test_effort_small_gap_chooses_lower_and_high_floor(self):
        model=self.models['codex_sol']
        model['official_performance']={'benchmarks':[dict(self.benchmark,effort=e,score=score) for e,score in [('medium',89.2),('high',89.5),('max',90)]]}
        self.assertEqual(r.evidence_effort(model,'code','medium','small',[]),'medium')
        self.assertEqual(r.evidence_effort(model,'code','high','small',[]),'high')
        model['official_performance']['benchmarks'][0]['score']=80
        model['official_performance']['benchmarks'][1]['score']=81
        self.assertEqual(r.evidence_effort(model,'code','medium','small',[]),'max')

    def test_weekly_and_new_model_trigger(self):
        now=dt.datetime(2026,10,3,tzinfo=dt.timezone.utc)
        data={'catalog_models':['gpt-6.1-sol'],'refreshed_at':(now-dt.timedelta(days=6)).isoformat()}
        self.assertFalse(b.due(data,['gpt-6.1-sol'],now))
        self.assertTrue(b.due(data,['gpt-6.1-sol','gpt-7-sol'],now))
        data['refreshed_at']=(now-dt.timedelta(days=7)).isoformat()
        self.assertTrue(b.due(data,['gpt-6.1-sol'],now))

    def test_livebench_exact_versions_efforts_and_missing_values(self):
        text='model,code_generation,code_completion\ngpt-6.1-sol-max,80.282,80.435\ngpt-6.1-sol-xhigh,78.873,82.609\ngpt-6-sol-max,83.099,80.435\ngpt-7-sol-max,,70\n'
        result=b.parse_livebench(text,{'Coding':['code_generation','code_completion']},'2026_06_25','source','now')
        self.assertEqual(len(result['gpt-6.1-sol']['benchmarks']),2)
        self.assertNotEqual(result['gpt-6-sol']['benchmarks'][0]['score'],result['gpt-6.1-sol']['benchmarks'][0]['score'])
        self.assertFalse(result['gpt-7-sol']['benchmarks'])

    def test_official_real_html_structure_no_competitor_reattribution(self):
        text='<table><tr><td></td><th>Opus 5.5</th><th>GPT-6 Astra</th></tr><tr><th><span class="benchmark">Terminal-Bench 4.0</span></th><td>66.4%</td><td>57.9%</td></tr></table>'
        values=b.parse_official(text,'claude-opus-5-5','source','now')
        self.assertEqual(values[0]['score'],66.4)
        self.assertFalse(values[0]['comparable'])
        self.assertFalse(b.parse_official(text,'gpt-6.1-sol','source','now'))

    def test_failure_retains_previous_values_and_generates_warning(self):
        original={'schema_version':1,'models':{'gpt-6.1-sol':{'benchmarks':[dict(self.benchmark,source='old',source_type='official')]}}}
        b.atomic_json(self.cache,original)
        with patch.object(b,'fetch',side_effect=OSError('offline')):
            result=b.refresh_benchmarks(['gpt-6.1-sol'],True,self.cache,self.table)
        data=b.read_cache(self.cache)
        self.assertEqual(result['status'],'retained_cache')
        self.assertEqual(data['models'],original['models'])
        self.assertTrue(data['warnings'])
        self.assertNotIn('refreshed_at',data)
        self.assertIn('손편집 금지',self.table.read_text())

    def test_schedule_never_fetches_or_waits(self):
        with patch.object(b,'read_cache',return_value={'models':{}}), patch.object(b,'fetch') as network, patch.object(b.subprocess,'Popen') as process, patch.object(b.threading,'Thread'):
            b.schedule_refresh(['gpt-7-sol'])
        network.assert_not_called()
        process.assert_called_once()
        self.assertIn('refresh-benchmarks',process.call_args[0][0])

    def test_fresh_command_has_no_network(self):
        data={'schema_version':1,'models':{},'catalog_models':['sol'],'refreshed_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        b.atomic_json(self.cache,data)
        with patch.object(b,'fetch') as network:
            self.assertEqual(b.refresh_benchmarks(['sol'],False,self.cache,self.table)['status'],'fresh_cache')
        network.assert_not_called()

    def test_missing_baseline_effort_does_not_promote_unmeasured_level(self):
        model=self.models['codex_sol']
        model['official_performance']={'benchmarks':[dict(self.benchmark,effort=e,score=90) for e in ('xhigh','max')]}
        self.assertEqual(r.evidence_effort(model,'code','medium','small',[]),'medium')

    def test_refresh_lock_keeps_concurrent_worker_out(self):
        import fcntl
        with self.cache.with_suffix('.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(b,'fetch') as network:
                self.assertEqual(b.refresh_benchmarks(['sol'],True,self.cache,self.table)['status'],'already_running')
            network.assert_not_called()

    def test_new_model_bypasses_failed_catalog_backoff(self):
        data={'schema_version':1,'models':{},'attempted_models':['sol'],
              'attempted_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        with patch.object(b,'read_cache',return_value=data), patch.object(b.subprocess,'Popen') as process, patch.object(b.threading,'Thread'):
            b.schedule_refresh(['sol'])
            process.assert_not_called()
            b.schedule_refresh(['sol','new'])
            process.assert_called_once()

    def test_partial_failure_preserves_official_values(self):
        previous=dict(self.benchmark,source='old-official',source_type='official')
        b.atomic_json(self.cache,{'schema_version':1,'models':{'gpt-6.1-sol':{'benchmarks':[previous]}}})
        def fetch(url):
            if url.endswith('robots.txt'): return 'User-agent: *\nDisallow:\n'
            if url==b.LIVE_INDEX: return '[{"name":"table_2026_06_25.csv"}]'
            if 'categories_' in url: return '{"Coding":["code_generation"]}'
            if 'table_' in url: return 'model,code_generation\ngpt-6.1-sol-max,80.282\n'
            raise OSError('official unavailable')
        with patch.object(b,'fetch',side_effect=fetch):
            result=b.refresh_benchmarks(['gpt-6.1-sol'],True,self.cache,self.table)
        values=b.read_cache(self.cache)['models']['gpt-6.1-sol']['benchmarks']
        self.assertIn(previous,values)
        self.assertEqual(result['received_items'],1)
        self.assertEqual(result['status'],'refreshed')

if __name__=='__main__':
    unittest.main()
