"""Additional adversarial acceptance; all writes confined to pytest temp dirs."""
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import research_core as core
from test_research_engine import args, read, events


def test_large_integer_final_is_not_rounded():
    value='9007199254740993'
    assert core.parse_final_strict('FINAL: '+value).value==value
    assert core.parse_final_strict('FINAL: '+('9'*500)).status=='ok'


@pytest.mark.parametrize('text',['FINAL: .5','FINAL: :5','FINAL: 5!!!','FINAL: option -2','FINAL: option 0'])
def test_malformed_tokens_are_not_normalized_into_answers(text):
    assert core.parse_final_strict(text).value is None


def test_cross_process_seed_zero_and_hash_order():
    code='import iq_bench,json; print(json.dumps(iq_bench.build_test(seed=0),sort_keys=True))'
    outputs=[]
    for seed in ['1','999']:
        p=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,
                         env={**os.environ,'PYTHONHASHSEED':seed,'PYTHONDONTWRITEBYTECODE':'1'})
        assert p.returncode==0
        outputs.append(p.stdout)
    assert outputs[0]==outputs[1]


def test_seed_zero_is_not_time(monkeypatch):
    import iq_bench
    monkeypatch.setattr(iq_bench.time,'time',lambda:1)
    first=iq_bench.build_test(seed=0)
    monkeypatch.setattr(iq_bench.time,'time',lambda:999999)
    assert iq_bench.build_test(seed=0)==first


@pytest.mark.parametrize('event_name',['attempt_result','champion_frozen','mutation_frozen','run_finished'])
def test_interrupt_each_durable_boundary(tmp_path,event_name):
    cfg=args(tmp_path)
    if event_name=='mutation_frozen': cfg['mode']='meta'
    def hook(ev):
        if ev['event']==event_name: raise KeyboardInterrupt()
    first=core.FixtureTransport()
    with pytest.raises(KeyboardInterrupt):
        core.run_experiment(**cfg,transport=first,event_hook=hook)
    second=core.FixtureTransport()
    result=core.run_experiment(**cfg,transport=second,resume=True)
    assert result['status']=='insufficient_data'
    assert len(first.calls)+len(second.calls)==result['usage']['attempts']
    assert result['usage']['attempts']<=8
    core.run_experiment(**cfg,transport=core.FixtureTransport(),resume=True)


@pytest.mark.parametrize('name',['prompts.json','manifest.json','checkpoint.json','result.json','events.jsonl'])
def test_artifact_tampering_fails_closed(tmp_path,name):
    cfg=args(tmp_path)
    core.run_experiment(**cfg,transport=core.FixtureTransport())
    path=cfg['run_dir']/name
    if name=='events.jsonl': path.write_text(path.read_text()+'{"partial":')
    else: path.write_text('{}')
    fixture=core.FixtureTransport()
    with pytest.raises(core.ResumeMismatch):
        core.run_experiment(**cfg,transport=fixture,resume=True)
    assert fixture.calls==[]


def test_zero_token_budget_sends_nothing(tmp_path):
    cfg=args(tmp_path,max_tokens=0)
    fixture=core.FixtureTransport()
    r=core.run_experiment(**cfg,transport=fixture)
    assert r['reason']=='budget_exhausted' and fixture.calls==[]


@pytest.mark.parametrize('http_status,calls',[(400,1),(401,1),(429,2),(503,2)])
def test_only_transient_http_retried(tmp_path,http_status,calls):
    cfg=args(tmp_path,max_calls=2)
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(http_status,text='URL SECRET must never be logged')
    with core.HttpxTransport('https://fixture.invalid','x',http_transport=httpx.MockTransport(handler)) as transport:
        core.run_experiment(**cfg,transport=transport)
    responses=[e for e in events(cfg['run_dir']) if e['event']=='attempt_result']
    first_key=responses[0]['item_key']
    assert sum(e['item_key']==first_key for e in responses)==calls
    assert all(e['raw'] is None for e in responses)


def test_unknown_usage_stays_reserved_and_blocks_promotion(tmp_path):
    cfg=args(tmp_path)
    class MissingUsage(core.FixtureTransport):
        def send(self,payload):
            data=super().send(payload).json(); del data['usage']
            return httpx.Response(200,json=data)
    r=core.run_experiment(**cfg,transport=MissingUsage())
    assert r['usage']['unknown_usage_attempts']==6  # P1-A: shared arm reduces distinct attempts
    assert r['usage']['charged_tokens']>0 and not r['usage']['actual_tokens_complete']
    assert r['status']=='blocked' and not r['promoted']


def test_actual_overrun_blocks(tmp_path):
    cfg=args(tmp_path,max_tokens=50000)
    class Overrun(core.FixtureTransport):
        def send(self,payload):
            data=super().send(payload).json()
            data['usage']={'prompt_tokens':50001,'completion_tokens':1,'total_tokens':50002}
            return httpx.Response(200,json=data)
    fixture=Overrun()
    r=core.run_experiment(**cfg,transport=fixture)
    assert len(fixture.calls)==1 and r['reason']=='token_budget_overrun'


def test_local_arc_revision_hashes_all_cases(tmp_path,monkeypatch):
    root=tmp_path/'arc'
    for name,color in [('training',1),('evaluation',2)]:
        folder=root/'data'/name; folder.mkdir(parents=True)
        (folder/'task.json').write_text(json.dumps({'train':[{'input':[[color]],'output':[[0]]}],
            'test':[{'input':[[color]],'output':[[0]]},{'input':[[color,color]],'output':[[0,0]]}]}))
    def revision(cmd,**kw):
        assert cmd[:1]==['git'] and cmd[-2:]==['rev-parse','HEAD']
        return subprocess.CompletedProcess(cmd,0,'a'*40+'\n','')
    monkeypatch.setattr(subprocess,'run',revision)
    split,data=core.arc_tasks(root,1,0,'a'*40)
    assert len(split['dev'])==len(split['heldout'])==2
    assert data['revision']=='a'*40 and len(data['files'])==2
    assert '2 2' not in split['heldout'][1]['question']  # compact grid renderer
    assert '\n22\n' in split['heldout'][1]['question']
    with pytest.raises(ValueError,match='revision'): core.arc_tasks(root,1,0,'b'*40)
    with pytest.raises(ValueError,match='insufficient'): core.arc_tasks(root,2,0)


def test_arc_full_task_requires_every_case(tmp_path):
    cfg=args(tmp_path); cfg.update(mode='arc',fixture=True)
    r=core.run_experiment(**cfg,transport=core.FixtureTransport())
    assert r['metrics']['baseline']['accuracy']==0.5
    assert r['arc']['baseline']['full_task_accuracy']==0.0
    assert r['paired']['n']==2


def test_source_read_once_and_full_snapshots_survive_midrun_change(tmp_path):
    cfg=args(tmp_path)
    path=cfg['source_root']/'prompt_catalog.json'
    fixture=core.FixtureTransport()
    def hook(ev):
        if ev['event']=='manifest_frozen': path.write_text('{}')
    core.run_experiment(**cfg,transport=fixture,event_hook=hook)
    systems=[m['content'] for p in fixture.calls for m in p['messages'] if m['role']=='system']
    assert systems and all(s=='Check all arithmetic. '*300 for s in systems)
    with pytest.raises(core.ResumeMismatch): core.run_experiment(**cfg,transport=fixture,resume=True)


def test_import_safe_without_files_or_http(tmp_path):
    script='''import pathlib, httpx, sys
sys.path.insert(0, sys.argv[1])
def forbidden(*a, **k): raise AssertionError("side effect")
pathlib.Path.mkdir=forbidden
httpx.Client=forbidden
import research_core, iq_research
'''
    p=subprocess.run([sys.executable,'-c',script,str(core.ROOT)],cwd=tmp_path,capture_output=True,text=True)
    assert p.returncode==0,p.stderr


def test_native_catalog_full_text_provenance_and_diversity(tmp_path):
    cfg=args(tmp_path)
    text='FULL SOURCE, not its extracted core. '*400
    entry={'text':text,'sha256':core.qhash(text),'source_repo':'public-A','source_path':'prompts/full.txt',
           'revision':'a'*40,'variant':'full_public','vendor':'A','core':'SHORT CORE','length':len(text)}
    other={**entry,'text':'Other vendor public text.','sha256':core.qhash('Other vendor public text.'),'vendor':'B','source_repo':'public-B'}
    path=cfg['source_root']/'prompt_catalog.json'
    path.write_text(json.dumps({'schema_version':1,'prompts':{'one':entry,'two':other}}))
    candidates=core.load_candidates(cfg['source_root'],2)
    assert candidates[1]['system']==text
    assert candidates[1]['provenance']['revision']=='a'*40
    assert candidates[1]['provenance']['source_path']=='prompts/full.txt'
    assert len({c['source'] for c in candidates[1:]})==2
    entry['sha256']='bad'
    path.write_text(json.dumps({'schema_version':1,'prompts':{'one':entry}}))
    with pytest.raises(ValueError,match='hash'): core.load_candidates(cfg['source_root'],1)


def test_legacy_arc_wrapper_scores_all_cases(monkeypatch):
    import arc_harness
    task={'train':[], 'test':[{'input':[[1]],'output':[[1]]},{'input':[[2]],'output':[[2]]}]}
    seen=[]
    def fake(*a,**kw):
        seen.append(kw['test_index'])
        return [[1]],'1'
    monkeypatch.setattr(arc_harness,'run_task',fake)
    assert not arc_harness.score_task('EXACT','unused','unused',task,None,'fixture')
    assert seen==[0,1]


def test_pair_metrics_and_gate_require_complete_errors_free_data(tmp_path):
    cfg=args(tmp_path); cfg.update(profile='standard')
    fixture=core.FixtureTransport()
    core.run_experiment(**cfg,transport=fixture)
    manifest=read(cfg['run_dir'],'manifest.json')
    ev=events(cfg['run_dir'])
    for e in ev:
        if e['event']=='evaluation':
            e['correct']=e['arm']!='baseline'
    champion=manifest['candidates'][1]['id']
    r=core.summarize(manifest,ev,None,champion)
    assert r['paired']['cells']['only_champion']==40
    assert r['paired']['delta']==1 and r['promoted']
    # P1-C: a transient attempt superseded by a retry must not poison the gate.
    # (Fixtures complete all evaluations; a lone transient in the journal is
    # by definition a retried-and-recovered attempt.)
    bad=next(e for e in ev if e['event']=='attempt_result')
    bad['status']='http_503'
    r=core.summarize(manifest,ev,None,champion)
    assert r['status'] in ('evidence_of_gain','no_evidence_of_gain')
    assert 'incomplete_usage' != r['reason']
    # A hard failure that stays (no retry) must still block:
    bad['status']='http_400'
    assert not core.summarize(manifest,ev,None,champion)['promoted']  # P1-C: recovered transient must not block
