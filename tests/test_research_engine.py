"""Isolated engine acceptance: no endpoints, credentials or production writes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_core as core


def corpus(tmp_path):
    root = tmp_path / 'corpus'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'prompt_catalog.json').write_text(json.dumps({'version': 1, 'prompts': [
        {'id': 'one', 'text': 'Check all arithmetic. ' * 300, 'source': 'public-A'},
        {'id': 'two', 'text': 'Validate every rule.', 'source': 'public-B'}]}))
    return root


def corpus_dir(tmp_path):
    return tmp_path / 'corpus'


def args(tmp_path, **kw):
    return dict(mode='boost', model='EXACT', profile='smoke', seed=0,
                run_dir=tmp_path/'run', source_root=corpus(tmp_path), **kw)


def read(run, name):
    return json.loads((Path(run)/name).read_text())


def events(run):
    return [json.loads(x) for x in (Path(run)/'events.jsonl').read_text().splitlines()]


def test_whole_workflow_snapshots_and_completed_resume(tmp_path):
    cfg = args(tmp_path)
    fixture = core.FixtureTransport()
    result = core.run_experiment(**cfg, transport=fixture)
    assert result['status'] == 'insufficient_data'
    assert len(fixture.calls) == 6  # P1-A: champion==baseline -> shared heldout arm
    manifest = read(cfg['run_dir'], 'manifest.json')
    assert manifest['planned_counts']['max_evaluations'] == 8
    assert not set(manifest['split']['dev']) & set(manifest['split']['holdout'])
    ev = events(cfg['run_dir'])
    freeze = next(i for i,e in enumerate(ev) if e['event'] == 'champion_frozen')
    assert all(i > freeze for i,e in enumerate(ev) if e['phase']=='heldout')
    prompts = read(cfg['run_dir'], 'prompts.json')
    assert prompts['candidates']['baseline']['system'] is None
    assert any(len(c['system'] or '') > 1000 for c in prompts['candidates'].values())
    for name in ['REPORT.md','meta_brain_live.jsonl','checkpoint.json','result.json']:
        assert (cfg['run_dir']/name).exists()
    before = (cfg['run_dir']/'manifest.json').read_bytes()
    again = core.FixtureTransport()
    assert core.run_experiment(**cfg, transport=again, resume=True) == result
    assert again.calls == []
    assert (cfg['run_dir']/'manifest.json').read_bytes() == before


def test_budget_has_planned_denominators_no_promotion(tmp_path):
    cfg = args(tmp_path, max_calls=1)
    fixture = core.FixtureTransport()
    r = core.run_experiment(**cfg, transport=fixture)
    assert len(fixture.calls)==1 and r['status']=='blocked'
    assert r['reason']=='budget_exhausted' and not r['promoted']
    assert r['overall']['planned']==8
    assert r['overall']['coverage'] <= 1/8


def test_meta_mutation_is_dev_only_and_budgeted(tmp_path):
    cfg=args(tmp_path); cfg['mode']='meta'
    fixture=core.FixtureTransport()
    r=core.run_experiment(**cfg,transport=fixture)
    assert r['status']=='insufficient_data'
    ev=events(cfg['run_dir'])
    mutation=next(e for e in ev if e['event']=='mutation_frozen')
    assert mutation['lineage'] and mutation['hypothesis_status']=='unproven'
    manifest=read(cfg['run_dir'],'manifest.json')
    held=[t['question'] for t in manifest['tasks']['heldout']]
    meta_calls=[p for p in fixture.calls if 'DEV-ONLY MUTATION' in p['messages'][-1]['content']]
    assert len(meta_calls)==1
    assert all(q not in json.dumps(meta_calls) for q in held)
    assert len(fixture.calls)<=8
    assert r['usage']['attempts']==len(fixture.calls)


def test_meta_no_mutation_blocks(tmp_path):
    cfg=args(tmp_path); cfg['mode']='meta'
    fixture=core.FixtureTransport(mutation=False)
    r=core.run_experiment(**cfg,transport=fixture)
    assert r['status']=='blocked' and r['reason']=='no_mutation'
    assert not any(e['phase']=='heldout' for e in events(cfg['run_dir']))


def test_durable_interruption_resume_never_replays_scored_item(tmp_path):
    cfg=args(tmp_path)
    def interrupt(ev):
        if ev['event']=='evaluation':
            raise KeyboardInterrupt()
    first=core.FixtureTransport()
    with pytest.raises(KeyboardInterrupt):
        core.run_experiment(**cfg,transport=first,event_hook=interrupt)
    second=core.FixtureTransport()
    r=core.run_experiment(**cfg,transport=second,resume=True)
    assert len(first.calls)+len(second.calls)==6  # P1-A: shared arm, no duplicates
    assert r['status']=='insufficient_data'
    # P1-B: interruption at manifest freeze must stay resumable.
    cfg2=args(tmp_path/'freeze')
    cfg2['run_dir']=tmp_path/'freeze'/'run'
    def freeze_interrupt(ev):
        if ev['event']=='manifest_frozen':
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        core.run_experiment(**cfg2,transport=core.FixtureTransport(),event_hook=freeze_interrupt)
    r2=core.run_experiment(**cfg2,transport=core.FixtureTransport(),resume=True)
    assert r2['status']=='insufficient_data'


def test_crash_after_reservation_fails_closed(tmp_path):
    cfg=args(tmp_path)
    def interrupt(ev):
        if ev['event']=='attempt_reserved':
            raise KeyboardInterrupt()
    fixture=core.FixtureTransport()
    with pytest.raises(KeyboardInterrupt):
        core.run_experiment(**cfg,transport=fixture,event_hook=interrupt)
    r=core.run_experiment(**cfg,transport=fixture,resume=True)
    assert fixture.calls==[]
    assert r['status']=='blocked' and r['reason']=='uncertain_attempt'
    assert r['usage']['attempts']==1


def test_source_and_config_fingerprint_fail_closed(tmp_path):
    cfg=args(tmp_path)
    core.run_experiment(**cfg,transport=core.FixtureTransport())
    frozen=(cfg['run_dir']/'prompts.json').read_bytes()
    with pytest.raises(core.ResumeMismatch):
        core.run_experiment(**{**cfg,'seed':1},transport=core.FixtureTransport(),resume=True)
    (cfg['source_root']/'prompt_catalog.json').write_text('{}')
    with pytest.raises(core.ResumeMismatch):
        core.run_experiment(**cfg,transport=core.FixtureTransport(),resume=True)
    assert (cfg['run_dir']/'prompts.json').read_bytes()==frozen


def test_lock(tmp_path):
    cfg=args(tmp_path)
    with core.RunWriter(cfg['run_dir']):
        with pytest.raises(core.ConcurrentRunError):
            core.run_experiment(**cfg,transport=core.FixtureTransport())


@pytest.mark.parametrize('failure,expected', [('length','truncated'),('wrong-model','model_mismatch'),('reasoning-only','no_final')])
def test_invalid_metadata_never_success(tmp_path,failure,expected):
    cfg=args(tmp_path)
    fixture=core.FixtureTransport(failure=failure)
    r=core.run_experiment(**cfg,transport=fixture)
    assert r['status']=='blocked' and not r['promoted']
    assert any(e['status']==expected for e in events(cfg['run_dir']))


def test_http_retry_attempt_metadata_and_budget_before_send(tmp_path):
    cfg=args(tmp_path,max_calls=2)
    received=[]
    def handle(request):
        ev=events(cfg['run_dir'])
        assert ev[-1]['event']=='attempt_reserved'
        assert (cfg['run_dir']/'manifest.json').exists()
        received.append(request)
        if len(received)==1:
            return httpx.Response(503)
        return httpx.Response(200,json={'model':'EXACT','choices':[{'finish_reason':'stop','message':{'content':'FINAL: -3','reasoning':'private diagnostic'}}], 'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}})
    with core.HttpxTransport('https://fixture.invalid/completions','SENTINEL_SECRET',http_transport=httpx.MockTransport(handle),ipv4=True) as transport:
        r=core.run_experiment(**cfg,transport=transport)
    assert len(received)==2 and r['usage']['attempts']==2
    assert r['usage']['actual_tokens']==16
    ev=events(cfg['run_dir'])
    responses=[e for e in ev if e['event']=='attempt_result']
    assert [e['http_status'] for e in responses]==[503,200]
    assert responses[-1]['reasoning']=='private diagnostic'
    for p in cfg['run_dir'].iterdir():
        if p.is_file():
            assert 'SENTINEL_SECRET' not in p.read_text()
            assert 'fixture.invalid' not in p.read_text()


@pytest.mark.parametrize('mode',['bench','boost','meta','arc'])
def test_actual_cli_fixture_all_modes(tmp_path,mode):
    run=tmp_path/mode
    cmd=[sys.executable,'-u',str(Path(core.__file__).with_name('iq_research.py')),'--mode',mode,'--model','EXACT','--profile','smoke','--run-dir',str(run),'--fixture']
    p=subprocess.run(cmd,capture_output=True,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
    assert p.returncode==0,p.stdout+p.stderr
    r=read(run,'result.json')
    assert r['status']=='insufficient_data'
    if mode=='arc':
        assert len(read(run,'manifest.json')['tasks']['heldout'])==2
        assert r['arc']['baseline']['planned_tasks']==1
    p=subprocess.run(cmd+['--resume'],capture_output=True,text=True)
    assert p.returncode==0,p.stdout+p.stderr
