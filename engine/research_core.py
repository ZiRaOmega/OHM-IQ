#!/usr/bin/env python3
"""Bounded generated-task research engine. See ENGINE_README.md for schema v1.

Journal reservations precede every send. An unacknowledged reservation is
terminal uncertainty, never an invitation to replay a potentially billed call.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

ENGINE_VERSION = "1.0.0"
SCHEMA_VERSION = 1

# ─────────────────────────────────────────────────────────────────────────────
# Strict FINAL parser
# ─────────────────────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
_OPT_RE = re.compile(r"^option[ \t]+([1-9][0-9]?)$", re.IGNORECASE)


@dataclass
class FinalAnswer:
    value: Optional[str]
    status: str   # ok | no_final | ambiguous | unparseable | truncated
    kind: Optional[str] = None  # numeric | option | valid_invalid

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _normalize_final_token(tok: str) -> Tuple[Optional[str], Optional[str]]:
    """Normalize one FINAL token -> (value, kind) or (None, None)."""
    t = tok.strip()
    if t.endswith('.'):
        t = t[:-1]  # one terminal period only; never strip leading punctuation
    if not t:
        return None, None
    m = _OPT_RE.match(t)
    if m:
        return m.group(1), "option"
    up = t.upper()
    if up in ("VALID", "INVALID"):
        return up, "valid_invalid"
    if _NUM_RE.match(t):
        # String normalization avoids float rounding/overflow and precision loss.
        sign = '-' if t.startswith('-') else ''
        integer, _, fraction = t.lstrip('+-').partition('.')
        integer = integer.lstrip('0') or '0'
        fraction = fraction.rstrip('0')
        if integer=='0' and not fraction: sign=''
        return sign+integer+('.'+fraction if fraction else ''), 'numeric'
    return None, None


def parse_final_strict(text: str, finish_reason: Optional[str] = None,
                       expected: Optional[str] = None) -> FinalAnswer:
    """Strict FINAL parser. `expected` is accepted but NEVER consulted
    (no answer-key dependent parsing). Tentative mentions of options in
    reasoning do not count. Truncation (finish_reason=length) is not a
    successful answer. Multiple differing finals fail closed."""
    if finish_reason == "length":
        return FinalAnswer(None, "truncated")
    if not isinstance(text,str) or not text.strip():
        return FinalAnswer(None, "no_final")
    if not re.match(r"^FINAL:", text.strip().splitlines()[-1].strip(), re.I):
        return FinalAnswer(None, "no_final")
    finals = re.findall(r"(?<![A-Za-z_])FINAL:[ \t]*([^\n]*)", text, re.I)
    if not finals:
        return FinalAnswer(None, "no_final")
    parsed = [_normalize_final_token(f) for f in finals]
    vals = {(v, k) for v, k in parsed if v is not None}
    bad = [f for f, (v, _) in zip(finals, parsed) if v is None]
    if bad and not vals:
        return FinalAnswer(None, "unparseable")
    if bad and vals:
        # some FINAL markers parse, others are prose -> ambiguous
        return FinalAnswer(None, "ambiguous")
    if len(vals) == 1:
        v, k = vals.pop()
        return FinalAnswer(v, "ok", k)
    return FinalAnswer(None, "ambiguous")


# ─────────────────────────────────────────────────────────────────────────────
# Strict ARC grid parser
# ─────────────────────────────────────────────────────────────────────────────

def strict_parse_grid(text: str) -> Tuple[Optional[List[List[int]]], str]:
    """Only a complete rectangular 1..30 grid of ASCII colors 0..9.

    Rows may be compact or whitespace-separated single digits. No prose,
    markdown fences, blank internal rows, JSON, signs, or digit harvesting.
    """
    if not isinstance(text,str) or not text.strip():
        return None, "empty"
    rows = []
    for ln in text.strip().splitlines():
        ln = ln.strip()
        if not ln:
            return None, "invalid"
        if re.fullmatch(r"[0-9]{1,30}", ln):
            row = [int(c) for c in ln]
        elif re.fullmatch(r"[0-9](?:[ \t]+[0-9]){0,29}", ln):
            row = [int(c) for c in ln.split()]
        else:
            return None, "invalid"
        rows.append(row)
    if not rows:
        return None, "invalid"
    w = len(rows[0])
    if any(len(r) != w for r in rows):
        return None, "invalid"
    if not (1 <= len(rows) <= 30 and 1 <= w <= 30):
        return None, "invalid"
    return rows, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic split + stats
# ─────────────────────────────────────────────────────────────────────────────

def qhash(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()


def dedup_items(items: Sequence[dict]) -> List[dict]:
    seen, out = set(), []
    for it in items:
        h = qhash(it["question"])
        if h not in seen:
            seen.add(h)
            out.append(it)
    return out


def split_items(items: Sequence[dict], dev_n: int, holdout_n: int,
                seed: int = 0) -> Tuple[List[dict], List[dict]]:
    """Deterministic, cross-process stable split (hash ordering, no
    PYTHONHASHSEED dependence). Exact question-hash dedup across sets."""
    uniq = dedup_items(items)
    if dev_n < 1 or holdout_n < 1 or len(uniq) < dev_n + holdout_n:
        raise ValueError("insufficient unique items for requested split")
    keyed = sorted(uniq, key=lambda it: hashlib.sha256(
        f"{seed}:{qhash(it['question'])}".encode()).hexdigest())
    return keyed[:dev_n], keyed[dev_n:dev_n + holdout_n]


def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial test on discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, m + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


def paired_bootstrap_delta(pairs: Sequence[Tuple[bool, bool]], n: int = 2000,
                           seed: int = 0) -> Dict[str, float]:
    """Deterministic paired bootstrap on (champion_correct, baseline_correct).
    Returns {"mean","lo","hi"} for the accuracy delta."""
    if not pairs:
        return {"mean": 0.0, "lo": 0.0, "hi": 0.0}
    rng = random.Random(seed)
    deltas = []
    m = len(pairs)
    for _ in range(n):
        samp = [pairs[rng.randrange(m)] for _ in range(m)]
        d = sum(1 for a, b in samp if a) - sum(1 for a, b in samp if b)
        deltas.append(d / m)
    deltas.sort()

    def pct(p):
        i = min(len(deltas) - 1, max(0, int(round(p * (len(deltas) - 1)))))
        return deltas[i]
    return {"mean": sum(int(a)-int(b) for a,b in pairs)/m,
            "lo": pct(0.025), "hi": pct(0.975)}


ROOT = Path(__file__).resolve().parent
USER_TEMPLATE = ('Solve this generated reasoning task. End your reply with a line '
                 'exactly FINAL: <signed number, VALID, INVALID, or option N>.\n\n{q}')
PROFILES = {
    'smoke': dict(dev_n=2, holdout_n=2, cand_cap=1, max_calls=8, max_tokens=100000, arc_n=1),
    'quick': dict(dev_n=6, holdout_n=10, cand_cap=2, max_calls=64, max_tokens=800000, arc_n=3),
    'standard': dict(dev_n=12, holdout_n=40, cand_cap=2, max_calls=160, max_tokens=2000000, arc_n=12),
}
GATE = dict(min_paired_n=30, alpha=0.05, min_delta=0.05,
            require_complete=True, require_no_errors=True,
            test='two-sided exact McNemar; bootstrap lower bound > 0')
NOTES = [
    'Generated-task performance; no standardized human IQ measurement.',
    'Public ARC local evaluation, not an official ARC score or an AGI measure.',
    'Prompt optimization is not weight tuning; mutation hypotheses are unproven.',
    'Public corpus contamination cannot be excluded; repeated public holdouts risk reuse/selection bias.',
    'Baseline and champion use identical heldout cases and per-call limits; retries consume the shared budget.',
    'Missing/invalid/error/truncated outputs remain in all-planned denominators; no guaranteed gain.',
    'Token reservations use a conservative UTF-8 byte estimate plus output limit; provider accounting may differ.',
    'Price unknown; no tariffs configured. Fixtures provide synthetic diagnostics only.',
]


def digest(obj):
    return qhash(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, obj):
    atomic_text(path, json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)+'\n')


def atomic_text(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.engine_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ConcurrentRunError(RuntimeError):
    pass


class ResumeMismatch(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


EVENT_FIELDS = dict(phase=None, candidate=None, task_id=None, family=None,
                    question=None, answer=None, expected=None, correct=None,
                    status=None, raw=None, reasoning=None, latency=None,
                    tokens=None, model_requested=None, model_returned=None,
                    finish_reason=None, attempts=None, http_status=None)


class RunWriter:
    def __init__(self, run_dir, event_hook=None):
        self.path = Path(run_dir)
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = (self.path/'.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ConcurrentRunError('run directory already locked') from None
        self.event_hook = event_hook
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def read(self, name):
        try:
            return json.loads((self.path/name).read_text())
        except (OSError,ValueError):
            raise ResumeMismatch('missing or corrupt artifact') from None

    def load_events(self):
        try:
            lines = (self.path/'events.jsonl').read_text().splitlines()
            self.events = [json.loads(line) for line in lines]
            previous = None
            for n, ev in enumerate(self.events):
                core = {k:v for k,v in ev.items() if k!='event_hash'}
                if ev['seq'] != n or ev['previous_hash'] != previous or digest(core)!=ev['event_hash']:
                    raise ValueError('journal integrity')
                previous = ev['event_hash']
        except (ValueError, KeyError, OSError):
            raise ResumeMismatch('journal missing, partial or corrupt; no calls permitted') from None

    def append(self, event, **fields):
        ev = dict(schema_version=SCHEMA_VERSION, ts=_utcnow(), event=event,
                  **EVENT_FIELDS)
        ev.update(fields)
        ev.update(seq=len(self.events), previous_hash=self.events[-1]['event_hash'] if self.events else None)
        ev['event_hash'] = digest(ev)
        self.append_line('events.jsonl', ev)
        self.events.append(ev)
        if self.event_hook:
            self.event_hook(ev)
        return ev

    def append_line(self, name, obj):
        with (self.path/name).open('a', encoding='utf-8') as f:
            f.write(json.dumps(obj, ensure_ascii=False, allow_nan=False)+'\n')
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(self.path)

    def checkpoint(self, manifest, prompts):
        checkpoint = dict(
            schema_version=SCHEMA_VERSION, fingerprint=manifest['fingerprint'],
            manifest_hash=digest(manifest), prompts_hash=digest(prompts),
            journal_head=self.events[-1]['event_hash'] if self.events else None,
            completed=[e['item_key'] for e in self.events if e['event']=='evaluation'],
            champion=next((e['selected'] for e in self.events if e['event']=='champion_frozen'), None),
            usage=usage_summary(self.events))
        checkpoint['checkpoint_hash']=digest(checkpoint)
        atomic_json(self.path/'checkpoint.json',checkpoint)
        # This is a derived live view. The journal is authoritative after crash.
        atomic_text(self.path/'meta_brain_live.jsonl',''.join(
            json.dumps(e,ensure_ascii=False)+'\n' for e in self.events if e['phase']=='meta'))

    def validate_checkpoint(self,manifest,prompts):
        checkpoint=self.read('checkpoint.json')
        heads={e['event_hash'] for e in self.events}
        # journal_head=None is valid iff the journal holds exactly the freeze
        # event written after that checkpoint (crash-during-freeze recovery).
        head_ok = checkpoint.get('journal_head') in heads or (
            checkpoint.get('journal_head') is None and
            [e['event'] for e in self.events]==['manifest_frozen'])
        if (checkpoint.get('checkpoint_hash')!=digest({k:v for k,v in checkpoint.items() if k!='checkpoint_hash'}) or
                checkpoint.get('fingerprint')!=manifest['fingerprint'] or
                checkpoint.get('manifest_hash')!=digest(manifest) or checkpoint.get('prompts_hash')!=digest(prompts) or
                not head_ok):
            raise ResumeMismatch('checkpoint integrity mismatch')


class HttpxTransport:
    """One send == one attempt. Retries and durable accounting belong to engine.

    An injected httpx.BaseTransport (e.g. MockTransport) never opens a socket.
    Endpoint/key stay private and are never included in errors or artifacts.
    """
    def __init__(self, base_url, api_key, *, ipv4=False, timeout=60.0, http_transport=None):
        import httpx
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self._url, self._key = base_url, api_key
        backend = http_transport if http_transport is not None else httpx.HTTPTransport(
            local_address='0.0.0.0' if ipv4 else None, retries=0)
        self._client = httpx.Client(transport=backend, timeout=httpx.Timeout(timeout, connect=min(10.,timeout)),
                                    follow_redirects=False, trust_env=False)

    def send(self, payload):
        return self._client.post(self._url, headers={'Authorization':f'Bearer {self._key}'}, json=payload)

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FixtureTransport:
    """Synthetic content, deliberately not an answer-key oracle. No network."""
    def __init__(self, mutation=True, failure=None):
        self.calls = []
        self.mutation, self.failure = mutation, failure

    def send(self, payload):
        import httpx
        self.calls.append(json.loads(json.dumps(payload)))
        text = payload['messages'][-1]['content']
        content = '0' if text.startswith('GRID PUZZLE') else 'FINAL: 0'
        if 'DEV-ONLY MUTATION' in text:
            content = json.dumps({'system_prompt':'Check each constraint independently before the final answer.',
                                  'hypothesis':'Independent checking may reduce transcription errors.'}) if self.mutation else '{}'
        message = {'content':content, 'reasoning':'Synthetic provider reasoning, never scored.'}
        if self.failure=='reasoning-only':
            message={'content':'', 'reasoning':'FINAL: 0'}
        return httpx.Response(200, json={
            'model':'OTHER' if self.failure=='wrong-model' else payload['model'],
            'choices':[{'finish_reason':'length' if self.failure=='length' else 'stop','message':message}],
            'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})


def _source_paths(root):
    catalog = root/'prompt_catalog.json'
    return [catalog] if catalog.exists() else [root/'boosters_leaked.py']


def load_candidates(root, cap):
    """Read public corpus once as data. Never execute/import corpus Python."""
    import ast
    entries = []
    catalog = root/'prompt_catalog.json'
    if catalog.exists():
        data = json.loads(catalog.read_text())
        if not isinstance(data,dict): raise ValueError('invalid catalog object')
        records=data.get('prompts')
        if isinstance(records,dict) and data.get('schema_version')==1:
            normalized=[]
            for key,e in records.items():
                if not isinstance(e,dict) or not isinstance(e.get('text'),str): raise ValueError('invalid catalog text')
                if e.get('sha256')!=qhash(e['text']): raise ValueError('catalog text hash mismatch')
                if not all(isinstance(e.get(k),str) and e[k] for k in ('source_repo','source_path')):
                    raise ValueError('catalog source provenance missing')
                provenance={k:e.get(k) for k in ('source_repo','source_path','revision','variant','vendor','sha256')}
                normalized.append(dict(id=key,text=e['text'],source=e['source_repo']+':'+str(e.get('vendor','unknown')),
                                       label='full public source snapshot' if e.get('variant')=='full_public' else 'public source; completeness unverified',provenance=provenance))
            records=normalized
        elif not isinstance(records,list) or data.get('version')!=1:
            raise ValueError('unsupported catalog schema; see ENGINE_README.md')
        for e in records:
            if not isinstance(e, dict) or not all(isinstance(e.get(k),str) and e[k].strip() for k in ('id','text','source')):
                raise ValueError('invalid catalog entry')
            entries.append(dict(key=e['id'], text=e['text'], source=e['source'],
                                label=e.get('label','public catalog text; completeness unverified'),
                                provenance=e.get('provenance',dict(source=e['source']))))
    else:
        path = root/'boosters_leaked.py'
        if path.exists():
            tree = ast.parse(path.read_text())
            # LEAKED is a literal dict of adapted compatibility cores. Ignore
            # executable/dynamic loaders and never infer that cores are full.
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='LEAKED' for t in node.targets):
                    data = ast.literal_eval(node.value)
                    for key, value in data.items():
                        if isinstance(value,str):
                            provider = key.removeprefix('leak_').split('_')[0]
                            if provider in ('o1','chatgpt'): provider='openai'
                            entries.append(dict(key=key,text=value,source=f'public compatibility core:{provider}',
                                                label='adapted excerpt from boosters_leaked.py:LEAKED; not full',
                                                provenance=dict(source_path='boosters_leaked.py',symbol='LEAKED',key=key,source_sha256=sha256_file(path))))
    unique, seen = [], set()
    for e in sorted(entries,key=lambda x:(x['source'],x['key'])):
        h = qhash(e['text'])
        if h not in seen:
            seen.add(h)
            unique.append(dict(id='seed_'+h[:16], system=e['text'], sha256=h,
                               source=e['source'], key=e['key'], label=e['label'],provenance=e['provenance']))
    # Source round-robin; second item from a source only after other sources.
    buckets = {}
    for e in unique:
        buckets.setdefault(e['source'],[]).append(e)
    selected=[]
    while buckets and len(selected)<cap:
        for src in list(buckets):
            selected.append(buckets[src].pop(0))
            if not buckets[src]: del buckets[src]
            if len(selected)==cap: break
    if cap and not selected:
        raise ValueError('no public seed candidates available')
    return [dict(id='baseline',system=None,sha256=digest(None),source='control',key='baseline',label='no system prompt')]+selected


def valid_grid(grid):
    return (isinstance(grid,list) and 1<=len(grid)<=30 and
            all(isinstance(row,list) and 1<=len(row)<=30 for row in grid) and
            len({len(row) for row in grid})==1 and
            all(type(c) is int and 0<=c<=9 for row in grid for c in row))


def arc_tasks(root, n, seed, revision=None):
    import subprocess
    from arc_harness import task_to_prompt
    result = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True)
    rev = result.stdout.strip()
    if result.returncode or not re.fullmatch('[0-9a-f]{40,64}',rev):
        raise ValueError('ARC requires a pinned local git revision; no automatic download')
    if revision is not None and revision != rev:
        raise ValueError('ARC revision mismatch')
    splits, hashes = {}, {}
    for phase, folder in [('dev','training'),('heldout','evaluation')]:
        paths = sorted((root/'data'/folder).glob('*.json'))
        if len(paths)<n:
            raise ValueError('insufficient ARC tasks')
        for p in paths: hashes[f'{folder}/{p.name}']=sha256_file(p)
        paths.sort(key=lambda p:digest([seed,folder,p.name]))
        items=[]
        for path in paths[:n]:
            task=json.loads(path.read_text())
            if not task.get('train') or not task.get('test'):
                raise ValueError('ARC task missing cases')
            for pair in task['train']+task['test']:
                if not valid_grid(pair.get('input')) or not valid_grid(pair.get('output')):
                    raise ValueError('ARC requires valid local inputs and scoring outputs')
            for index, pair in enumerate(task['test']):
                items.append(dict(question=task_to_prompt(task,index),answer=pair['output'],family='arc',
                                  task_group=f'{folder}/{path.stem}', test_index=index,
                                  data_sha256=hashes[f'{folder}/{path.name}']))
        splits[phase]=items
    return splits, dict(revision=rev, files=hashes, snapshot='local bytes; revision plus hashes, working tree may differ')


def fixture_arc():
    from arc_harness import task_to_prompt
    splits={}
    for phase,color in [('dev',1),('heldout',2)]:
        task={'train':[{'input':[[color]],'output':[[0]]}],
              'test':[{'input':[[color]],'output':[[0]]}]}
        if phase=='heldout': task['test'].append({'input':[[color,color]],'output':[[0,0]]})
        splits[phase]=[dict(question=task_to_prompt(task,i),answer=p['output'],family='arc',
                            task_group='fixture/'+phase,test_index=i,data_sha256=digest(task))
                       for i,p in enumerate(task['test'])]
    return splits, dict(revision='synthetic-fixture-v1',files={},snapshot='synthetic, not ARC corpus')


def build_manifest(config, root, arc_root, arc_revision):
    from iq_bench import build_test
    from iq_bench_hard import build_hard_test
    profile=PROFILES[config['profile']]
    cap=0 if config['mode']=='bench' else profile['cand_cap']
    candidates=load_candidates(root,cap)
    dev_n,hold_n=profile['dev_n'],profile['holdout_n']
    if config['mode']=='meta' and config['profile']=='smoke': dev_n,hold_n=1,1
    if config['mode']=='arc':
        splits,data=fixture_arc() if config['fixture'] else arc_tasks(arc_root,profile['arc_n'],config['seed'],arc_revision)
    else:
        # v2 hard bank: ceiling-proof families b∈[135,168], solver-verified unique
        from iq_bench_hard import HARD_FAMILIES
        n_needed=max(20,dev_n+hold_n)
        per=max(3,-(-n_needed//len(HARD_FAMILIES)) )
        items=build_hard_test(n_per_family=per,seed=config['seed'])
        items=items[:n_needed] if len(items)>=n_needed else items+build_hard_test(n_per_family=per+1,seed=config['seed']+1)[:n_needed-len(items)]
        dev,held=split_items(items,dev_n,hold_n,config['seed'])
        splits=dict(dev=dev,heldout=held)
        data=dict(generator='iq_bench_hard.build_hard_test',hash=digest(splits))
    for phase, items in splits.items():
        for i,t in enumerate(items):
            t['question_hash']=qhash(t['question'])
            t['id']=f'{phase}:{i}:{t["question_hash"][:16]}'
            t['user']=t['question'] if config['mode']=='arc' else USER_TEMPLATE.format(q=t['question'])
            t['user_sha256']=qhash(t['user'])
    dh={t['question_hash'] for t in splits['dev']}
    hh={t['question_hash'] for t in splits['heldout']}
    if dh & hh: raise ValueError('cross-split question duplicate')
    counts=dict(dev_cases=len(splits['dev']),heldout_cases=len(splits['heldout']),seed_candidates=len(candidates),
                mutation_slots=int(config['mode']=='meta'))
    counts['seed_dev_evaluations']=counts['dev_cases']*len(candidates)
    counts['mutation_dev_evaluations']=counts['dev_cases']*counts['mutation_slots']
    counts['heldout_evaluations']=2*counts['heldout_cases']
    counts['max_evaluations']=counts['seed_dev_evaluations']+counts['mutation_dev_evaluations']+counts['heldout_evaluations']
    counts['planned_calls_without_retries']=counts['max_evaluations']+counts['mutation_slots']
    counts['max_attempts_with_retries']=min(config['max_calls'],counts['planned_calls_without_retries']*(1+config['max_retries']))
    return dict(schema_version=SCHEMA_VERSION,engine_version=ENGINE_VERSION,created=_utcnow(),
                **config,planned_counts=counts,gate=GATE, candidates=candidates,tasks=splits,
                split=dict(dev=sorted(dh),holdout=sorted(hh)),data=data,notes=NOTES)


def fingerprints(config, root, arc_root):
    code={name:sha256_file(ROOT/name) for name in ['research_core.py','iq_research.py','iq_bench.py','arc_harness.py']}
    sources={str(p.relative_to(root)):sha256_file(p) if p.exists() else None for p in _source_paths(root)}
    arc={}
    if config['mode']=='arc' and not config['fixture']:
        import subprocess
        rev=subprocess.run(['git','-C',str(arc_root),'rev-parse','HEAD'],capture_output=True,text=True)
        arc['revision']=rev.stdout.strip() if not rev.returncode else None
        arc['files']={str(p.relative_to(arc_root)):sha256_file(p) for p in sorted((arc_root/'data').glob('*/*.json'))}
    components=dict(code=code,config=digest(config),sources=sources,arc=arc)
    return dict(sha256=digest(components),components=components)


TRANSIENT_STATUSES=frozenset({'http_429','http_500','http_502','http_503','http_504',
                              'ConnectError','ConnectTimeout','ReadTimeout','WriteTimeout',
                              'ReadError','WriteError','RemoteProtocolError','PoolTimeout'})


def usage_summary(events):
    reservations=[e for e in events if e['event']=='attempt_reserved']
    results={e['attempt_id']:e for e in events if e['event']=='attempt_result'}
    charged=actual=0
    unknown=0
    for e in reservations:
        r=results.get(e['attempt_id'])
        usage=r.get('tokens') if r else None
        if usage is not None and usage.get('total_tokens') is not None:
            actual+=usage['total_tokens']
            charged+=usage['total_tokens']
        elif r is not None and r.get('status') in TRANSIENT_STATUSES:
            # Transient attempt superseded by a retry: charge stays bounded by
            # the reservation; a recovered retry is not unknown usage.
            charged+=e['reserved_tokens']
        else:
            charged+=e['reserved_tokens']
            unknown+=1
    return dict(attempts=len(reservations), actual_tokens=actual, charged_tokens=charged,
                unknown_usage_attempts=unknown, actual_tokens_complete=unknown==0,
                latency_seconds=sum(e['latency'] or 0 for e in results.values()),
                price=None, price_status='unknown')


def _response_data(response, model):
    status=response.status_code
    fields=dict(http_status=status,raw=None,reasoning=None,tokens=None,
                model_returned=None,finish_reason=None,status=f'http_{status}')
    if status != 200:
        # Never persist error bodies: they may echo a URL/key/request headers.
        return fields
    try:
        data=response.json()
        choice=data['choices'][0]
        message=choice['message']
        raw=message.get('content') or ''
        reasoning=message.get('reasoning_content') or message.get('reasoning') or None
        if not isinstance(raw,str) or (reasoning is not None and not isinstance(reasoning,str)):
            raise ValueError()
        fields.update(raw=raw,reasoning=reasoning,model_returned=data.get('model'),finish_reason=choice.get('finish_reason'))
        usage=data.get('usage')
        if isinstance(usage,dict):
            keys=('prompt_tokens','completion_tokens','total_tokens')
            if all(type(usage.get(k)) is int and usage[k]>=0 for k in keys) and usage['total_tokens']>=usage['prompt_tokens']+usage['completion_tokens']:
                fields['tokens']={k:usage[k] for k in keys}
        if fields['model_returned'] != model: fields['status']='model_mismatch'
        elif fields['finish_reason']=='length': fields['status']='truncated'
        elif fields['finish_reason']!='stop': fields['status']='invalid_finish'
        else: fields['status']='ok'
    except (ValueError,KeyError,IndexError,TypeError):
        fields['status']='malformed_response'
    return fields


def _request(writer, manifest, transport, item_key, context, model, messages):
    """Replay durable responses; reserve and charge every new attempt first."""
    import httpx
    payload=dict(model=model,messages=messages,temperature=0.0,max_tokens=manifest['output_tokens'])
    previous=[e for e in writer.events if e['event']=='attempt_result' and e['item_key']==item_key]
    transient={'http_429','http_500','http_502','http_503','http_504','ConnectError','ConnectTimeout','ReadTimeout','WriteTimeout','ReadError','WriteError','RemoteProtocolError','PoolTimeout'}
    if previous and (previous[-1]['status'] not in transient or len(previous)>manifest['max_retries']):
        return previous[-1]
    while True:
        usage=usage_summary(writer.events)
        reserve=len(json.dumps(messages,ensure_ascii=False).encode('utf-8'))+256+manifest['output_tokens']
        if usage['attempts']>=manifest['max_calls'] or usage['charged_tokens']+reserve>manifest['max_tokens']:
            raise Blocked('budget_exhausted')
        attempt=usage['attempts']+1
        writer.append('attempt_reserved',**context,item_key=item_key,attempt_id=attempt,
                      attempts=len(previous)+1,model_requested=model, reserved_tokens=reserve,
                      effective_messages=messages,messages_sha256=digest(messages),status='reserved')
        t0=time.monotonic()
        try:
            response=transport.send(payload)
            fields=_response_data(response,model)
        except httpx.TransportError as exc:
            fields=dict(status=type(exc).__name__)
        except Exception:
            # Unknown injected/provider failure: do not retry or log exception text.
            fields=dict(status='transport_error')
        event=writer.append('attempt_result',**context,item_key=item_key,attempt_id=attempt,
                            attempts=len(previous)+1,model_requested=model,
                            latency=time.monotonic()-t0,**fields)
        previous.append(event)
        if event['status'] not in transient or len(previous)>manifest['max_retries']:
            return event


def _evaluate(writer, manifest, transport, phase, arm, candidate, task):
    item_key=f'{phase}/{arm}/{task["id"]}'
    done=next((e for e in writer.events if e['event']=='evaluation' and e['item_key']==item_key),None)
    if done: return done
    ctx=dict(phase=phase,candidate=candidate['id'],task_id=task['id'],family=task['family'],question=task['question'])
    messages=[]
    if candidate['system'] is not None: messages.append(dict(role='system',content=candidate['system']))
    messages.append(dict(role='user',content=task['user']))
    r=_request(writer,manifest,transport,item_key,ctx,manifest['model'],messages)
    value=None
    status=r['status']
    if status=='ok':
        if manifest['mode']=='arc': value,status=strict_parse_grid(r['raw'])
        else:
            parsed=parse_final_strict(r['raw'],finish_reason=r['finish_reason'])
            value,status=parsed.value,parsed.status
    return writer.append('evaluation',**ctx,item_key=item_key,arm=arm,
                         answer=value,expected=task['answer'],correct=status=='ok' and value==task['answer'],status=status,
                         **{k:r[k] for k in ['raw','reasoning','latency','tokens','model_requested','model_returned','finish_reason','attempts','http_status']})


def interleaved(tasks,candidates,seed):
    rng=random.Random(seed)
    for task in tasks:
        order=list(candidates)
        rng.shuffle(order)
        for candidate in order: yield task,candidate


def _mutation(writer,manifest,transport,candidates):
    existing=next((e for e in writer.events if e['event']=='mutation_frozen'),None)
    if existing: return existing['snapshot']
    dev=[e for e in writer.events if e['event']=='evaluation' and e['phase']=='dev']
    parents=[dict(id=c['id'],system=c['system'],sha256=c['sha256']) for c in candidates]
    # Exact dev questions/errors only; no manifest/tasks/holdout passed to mutator.
    examples=[{k:e[k] for k in ['candidate','question','answer','expected','correct','status']} for e in dev]
    prompt='DEV-ONLY MUTATION\nPropose one system prompt to improve these development tasks. '
    prompt+='Return only JSON {"system_prompt": "...", "hypothesis": "..."}. Hypothesis is unproven.\n'
    prompt+=json.dumps(dict(parents=parents,dev_results=examples),ensure_ascii=False)
    ctx=dict(phase='meta',candidate='mutation',task_id='mutation',family='meta',question=prompt)
    result=_request(writer,manifest,transport,'meta/mutation',ctx,manifest['meta_model'],[dict(role='user',content=prompt)])
    if result['status']!='ok': raise Blocked('no_mutation')
    try:
        proposal=json.loads(result['raw'])
        text,hypothesis=proposal['system_prompt'],proposal['hypothesis']
        if not isinstance(text,str) or not text.strip() or not isinstance(hypothesis,str) or not hypothesis.strip():
            raise ValueError()
        if qhash(text) in {c['sha256'] for c in candidates}: raise ValueError()
    except (ValueError,KeyError,TypeError):
        raise Blocked('no_mutation') from None
    snapshot=dict(id='mutation',system=text,sha256=qhash(text),source='dev-only proposal',key='mutation',label='unproven adapted prompt')
    writer.append('mutation_frozen',phase='meta',candidate='mutation',snapshot=snapshot,
                     lineage=[c['id'] for c in candidates],hypothesis=hypothesis,hypothesis_status='unproven',status='ok')
    return snapshot


def _metrics(rows,planned):
    ok=sum(bool(e['correct']) for e in rows)
    valid=sum(e['status']=='ok' for e in rows)
    trunc=sum(e['status']=='truncated' for e in rows)
    errors=len(rows)-valid
    return dict(planned=planned,recorded=len(rows),correct=ok,accuracy=ok/planned if planned else 0.,
                coverage=valid/planned if planned else 0.,completion_rate=len(rows)/planned if planned else 0.,
                error_rate=errors/planned if planned else 0.,truncation_rate=trunc/planned if planned else 0.,
                missing=planned-len(rows),valid_wrong=valid-ok,wilson=list(wilson_ci(ok,planned)))


def summarize(manifest,events,reason,champion):
    evaluations=[e for e in events if e['event']=='evaluation']
    held=manifest['tasks']['heldout']
    by={(e['arm'],e['task_id']):e for e in evaluations if e['phase']=='heldout'}
    pairs=[]
    cells=dict(both_correct=0,only_champion=0,only_baseline=0,both_wrong=0)
    for task in held:
        a=bool(by.get(('champion',task['id']),{}).get('correct'))
        b=bool(by.get(('baseline',task['id']),{}).get('correct'))
        pairs.append((a,b))
        cells['both_correct' if a and b else 'only_champion' if a else 'only_baseline' if b else 'both_wrong']+=1
    paired=dict(cells=cells,n=len(held),delta=sum(int(a)-int(b) for a,b in pairs)/len(pairs),
                bootstrap=paired_bootstrap_delta(pairs,seed=manifest['seed']),
                mcnemar_p=mcnemar_exact(cells['only_champion'],cells['only_baseline']))
    metrics={arm:_metrics([e for e in evaluations if e['phase']=='heldout' and e['arm']==arm],len(held))
             for arm in ['baseline','champion']}
    families={}
    for family in sorted({t['family'] for t in held}):
        families[family]={arm:_metrics([e for e in evaluations if e['phase']=='heldout' and e['arm']==arm and e['family']==family],sum(t['family']==family for t in held)) for arm in metrics}
    planned=manifest['planned_counts']['max_evaluations']
    complete=len(evaluations)==planned
    usage=usage_summary(events)
    if usage['charged_tokens']>manifest['max_tokens']: reason='token_budget_overrun'
    if reason is None and (not complete or any(e['status']!='ok' for e in evaluations)): reason='incomplete_or_invalid'
    if reason is None and usage['unknown_usage_attempts']: reason='incomplete_usage'
    if reason is None and any(e['status'] not in TRANSIENT_STATUSES and e['status']!='ok' for e in events if e['event']=='attempt_result'): reason='attempt_failure'
    if reason: status='blocked'
    elif manifest['profile']=='smoke' or len(held)<manifest['gate']['min_paired_n']: status='insufficient_data'
    elif champion != 'baseline' and paired['delta']>=manifest['gate']['min_delta'] and paired['bootstrap']['lo']>0 and paired['mcnemar_p']<manifest['gate']['alpha']:
        status='evidence_of_gain'
    else: status='no_evidence_of_gain'
    arc={}
    if manifest['mode']=='arc':
        groups=sorted({t['task_group'] for t in held})
        for arm in metrics:
            outcomes={group:all(by.get((arm,t['id']),{}).get('correct',False) for t in held if t['task_group']==group) for group in groups}
            arc[arm]=dict(planned_tasks=len(groups),correct_tasks=sum(outcomes.values()),
                          full_task_accuracy=sum(outcomes.values())/len(groups),tasks=outcomes,per_case=metrics[arm])
        # Cases within a task are dependent. Case-level uncertainty is diagnostic.
        if status=='evidence_of_gain': status='insufficient_data'
    return dict(schema_version=SCHEMA_VERSION,status=status,reason=reason,complete=complete and not reason,
                promoted=status=='evidence_of_gain',champion=champion,baseline='baseline',
                paired=paired,metrics=metrics,families=families,arc=arc,
                overall=_metrics(evaluations,planned),usage=usage,gate=manifest['gate'],notes=NOTES,
                fixture=manifest['fixture'],fingerprint=manifest['fingerprint'])


def run_experiment(*, mode, model, run_dir, profile='quick', seed=0, resume=False,
                   max_calls=None, max_tokens=None, meta_model=None, transport=None,
                   source_root=None, arc_dir=None, arc_revision=None, fixture=False,
                   output_tokens=None, max_retries=1, ipv4=False, timeout=None,
                   event_hook=None, transport_id='chat-completions-v1'):
    """Run synchronously. Inject transport.send(payload)->httpx.Response, one attempt.

    event_hook receives an event AFTER fsync (test interruption hook). No workers.
    Caller owns injected transport; engine closes its own persistent HTTP client.
    Mode-aware defaults: ARC grids + reasoning channels need far larger output
    budgets and timeouts than generated IQ items (legacy arc_harness used
    max_tokens=16000/timeout=600).
    """
    if output_tokens is None: output_tokens=16000 if mode=='arc' else 1024
    if timeout is None: timeout=600.0  # measured: nemotron-3-ultra on ollama.com ~250s/call; 60s guarantees ReadTimeout
    if mode not in ('bench','boost','meta','arc') or profile not in PROFILES or not model or not model.strip():
        raise ValueError('invalid mode/profile/exact model')
    if type(seed) is not int or not 0<=max_retries<=2 or output_tokens<1 or not math.isfinite(timeout) or timeout<=0:
        raise ValueError('invalid limits')
    root=Path(source_root or ROOT).resolve()
    arc_root=Path(arc_dir or ROOT/'arc_data').resolve()
    p=PROFILES[profile]
    config=dict(mode=mode,model=model,meta_model=meta_model or model,profile=profile,seed=seed,
                max_calls=p['max_calls'] if max_calls is None else max_calls,
                max_tokens=p['max_tokens'] if max_tokens is None else max_tokens,
                output_tokens=output_tokens,max_retries=max_retries,ipv4=ipv4,timeout=timeout,
                fixture=fixture,transport_id=transport_id,arc_revision_requested=arc_revision)
    if config['max_calls']<0 or config['max_tokens']<0: raise ValueError('negative budget')
    if profile=='smoke': config['max_calls']=min(config['max_calls'],8)
    owned=None
    with RunWriter(run_dir,event_hook) as writer:
        fp=fingerprints(config,root,arc_root)
        if (writer.path/'manifest.json').exists():
            if not resume: raise ResumeMismatch('existing manifest requires --resume')
            manifest=writer.read('manifest.json')
            if manifest.get('fingerprint')!=fp: raise ResumeMismatch('code/config/data fingerprint changed')
            writer.load_events()
            if not writer.events or writer.events[0]['event']!='manifest_frozen' or writer.events[0]['manifest_hash']!=digest(manifest):
                raise ResumeMismatch('manifest integrity mismatch')
            prompts=writer.read('prompts.json')
            if digest(prompts)!=manifest['prompts_sha256']: raise ResumeMismatch('prompt snapshot changed')
            writer.validate_checkpoint(manifest,prompts)
            if (writer.path/'result.json').exists():
                result=writer.read('result.json')
                terminal=next((e for e in writer.events if e['event']=='run_finished'),None)
                if terminal is None or terminal['result_hash']!=digest(result): raise ResumeMismatch('result integrity mismatch')
                return result
        else:
            if resume: raise ResumeMismatch('resume requires explicit existing manifest')
            if any(p.name not in ('.lock','run.log') and not p.name.startswith('.engine_') for p in writer.path.iterdir()): raise ResumeMismatch('run directory must be empty')
            manifest=build_manifest(config,root,arc_root,arc_revision)
            # Rehash after reads to reject concurrent corpus/code changes while freezing.
            if fp!=fingerprints(config,root,arc_root): raise ResumeMismatch('source changed while freezing')
            manifest['fingerprint']=fp
            prompts=dict(schema_version=SCHEMA_VERSION,
                         candidates={c['id']:c for c in manifest['candidates']},
                         users={t['id']:dict(text=t['user'],sha256=t['user_sha256']) for ts in manifest['tasks'].values() for t in ts},
                         mutation_slot=dict(system=None,source='future dev-only proposal; immutable snapshot recorded in mutation_frozen event'))
            manifest['prompts_sha256']=digest(prompts)
            atomic_json(writer.path/'prompts.json',prompts)
            atomic_json(writer.path/'manifest.json',manifest)
            atomic_text(writer.path/'events.jsonl','')
            atomic_text(writer.path/'meta_brain_live.jsonl','')
            # Checkpoint BEFORE the first journal append: a crash exactly at
            # manifest_frozen must still leave a resumable run.
            writer.checkpoint(manifest,prompts)
            writer.append('manifest_frozen',manifest_hash=digest(manifest),status='frozen')
        writer.checkpoint(manifest,prompts)
        reason=None
        champion=next((e['selected'] for e in writer.events if e['event']=='champion_frozen'),None)
        try:
            reservations={e['attempt_id'] for e in writer.events if e['event']=='attempt_reserved'}
            responses={e['attempt_id'] for e in writer.events if e['event']=='attempt_result'}
            if reservations-responses: raise Blocked('uncertain_attempt')
            if transport is None:
                if fixture: transport=FixtureTransport()
                else:
                    from iq_bench import load_endpoints
                    try:
                        endpoints=load_endpoints()
                    except Exception:
                        raise Blocked('endpoint_configuration_error') from None
                    if model not in endpoints or config['meta_model'] not in endpoints: raise Blocked('model_endpoint_missing')
                    if mode=='meta' and endpoints[model]!=endpoints[config['meta_model']]: raise Blocked('meta_endpoint_mismatch')
                    url,key=endpoints[model]
                    if not key: raise Blocked('credentials_missing')
                    owned=transport=HttpxTransport(url,key,ipv4=ipv4,timeout=timeout)
            candidates=list(manifest['candidates'])
            for task,candidate in interleaved(manifest['tasks']['dev'],candidates,seed):
                _evaluate(writer,manifest,transport,'dev',candidate['id'],candidate,task)
                writer.checkpoint(manifest,prompts)
            if any(e['status']!='ok' for e in writer.events if e['event']=='evaluation' and e['phase']=='dev'):
                raise Blocked('invalid_development')
            if mode=='meta':
                child=_mutation(writer,manifest,transport,candidates)
                candidates.append(child)
                for task in manifest['tasks']['dev']:
                    _evaluate(writer,manifest,transport,'dev',child['id'],child,task)
                    writer.checkpoint(manifest,prompts)
                if any(e['status']!='ok' for e in writer.events if e['event']=='evaluation' and e['phase']=='dev'):
                    raise Blocked('invalid_development')
            if champion is None:
                scores={c['id']:sum(bool(e['correct']) for e in writer.events if e['event']=='evaluation' and e['phase']=='dev' and e['candidate']==c['id']) for c in candidates}
                champion=min(candidates,key=lambda c:(-scores[c['id']],c['id']!='baseline',c['id']))['id']
                writer.append('champion_frozen',phase='selection',selected=champion,dev_scores=scores,status='frozen',
                              snapshot=next(c for c in candidates if c['id']==champion))
                writer.checkpoint(manifest,prompts)
            winner=next(c for c in candidates if c['id']==champion)
            if champion=='baseline' or winner==candidates[0]:
                # Champion IS the control arm: one shared evaluation per heldout
                # task serves both arms. No duplicate identical payloads, no
                # provider-noise fake discordant pairs.
                if not any(e['event']=='champion_is_baseline' for e in writer.events):
                    writer.append('champion_is_baseline',phase='selection',selected=champion,status='shared_arm',
                                  snapshot=winner)
                writer.checkpoint(manifest,prompts)
                for task in manifest['tasks']['heldout']:
                    ev=_evaluate(writer,manifest,transport,'heldout','baseline',candidates[0],task)
                    shared_key=f"heldout/champion/{task['id']}"
                    if not any(e['event']=='evaluation' and e.get('item_key')==shared_key for e in writer.events):
                        meta={'schema_version','ts','event','seq','previous_hash','event_hash'}
                        shared={k:v for k,v in ev.items() if k not in meta}
                        shared['arm']='champion'; shared['candidate']=champion; shared['item_key']=shared_key
                        writer.append('evaluation',**shared)
                    writer.checkpoint(manifest,prompts)
            else:
                arms=[dict(candidates[0],arm='baseline'),dict(winner,arm='champion')]
                for task,candidate in interleaved(manifest['tasks']['heldout'],arms,seed+1):
                    _evaluate(writer,manifest,transport,'heldout',candidate['arm'],candidate,task)
                    writer.checkpoint(manifest,prompts)
        except Blocked as exc:
            reason=str(exc)
            writer.append('blocked',status='blocked',reason=reason)
        finally:
            if owned: owned.close()
        result=summarize(manifest,writer.events,reason,champion)
        writer.checkpoint(manifest,prompts)
        report=(f'# Research run — {result["status"]}\n\n'
                f'Model: {model}. Profile: {profile}. Fixture: {fixture}.\n\n'
                f'Frozen DEV champion: {champion}. Reason: {result["reason"]}.\n\n'
                f'Paired heldout n={result["paired"]["n"]}; delta={result["paired"]["delta"]:.4f}; '
                f'exact paired p={result["paired"]["mcnemar_p"]:.6f}.\n\n'
                f'Overall planned rows: {result["overall"]["planned"]}; valid coverage: {result["overall"]["coverage"]:.3f}.\n\n'
                f'Actual reported tokens: {result["usage"]["actual_tokens"]}; unknown usage attempts: {result["usage"]["unknown_usage_attempts"]}; '
                f'attempts: {result["usage"]["attempts"]}; measured request time: {result["usage"]["latency_seconds"]:.3f}s; price unknown.\n\n'
                +'\n'.join('- '+note for note in NOTES)+'\n\n'
                'ARC case-level intervals are diagnostic because cases within a task are dependent; ARC promotion is disabled.\n')
        atomic_text(writer.path/'REPORT.md',report)
        writer.append('run_finished',status=result['status'],result_hash=digest(result))
        atomic_json(writer.path/'result.json',result)
        return result
