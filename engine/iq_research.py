#!/usr/bin/env python3
"""Import-safe bounded research CLI. --fixture never loads endpoints."""
import argparse
import json

from research_core import run_experiment, ConcurrentRunError, ResumeMismatch


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--mode',required=True,choices=['bench','boost','meta','arc'])
    ap.add_argument('--model',required=True)
    ap.add_argument('--profile',choices=['smoke','quick','standard'],default='quick')
    ap.add_argument('--run-dir',required=True)
    ap.add_argument('--seed',type=int,default=0)
    ap.add_argument('--resume',action='store_true')
    ap.add_argument('--max-calls',type=int)
    ap.add_argument('--max-tokens',type=int)
    ap.add_argument('--meta-model')
    ap.add_argument('--fixture',action='store_true',help='synthetic in-memory transport, no model network or credentials')
    ap.add_argument('--ipv4',action='store_true')
    ap.add_argument('--timeout',type=float,default=None)  # None → engine default (600s; ollama.com measured ~250s/call)
    ap.add_argument('--arc-dir')
    ap.add_argument('--arc-revision')
    ap.add_argument('--source-root')
    ap.add_argument('--output-tokens',type=int,default=1024)
    ap.add_argument('--max-retries',type=int,default=1)
    args=vars(ap.parse_args(argv))
    try:
        result=run_experiment(**args)
    except (ConcurrentRunError,ResumeMismatch,ValueError,OSError):
        # Exception text can include endpoint/config contents: never print it.
        print(json.dumps({'status':'blocked','reason':'configuration_or_integrity_error'}))
        return 2
    print(json.dumps({'status':result['status'],'reason':result['reason'],
                      'run_dir':args['run_dir'],'attempts':result['usage']['attempts']}))
    return 2 if result['status']=='blocked' else 0


if __name__=='__main__':
    raise SystemExit(main())
