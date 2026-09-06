#!/usr/bin/env python3
"""ARC-AGI OFFICIAL HARNESS — public corpus (fchollet/ARC), half-split protocol.

The official AGI reference test: 400 training + 400 evaluation grid puzzles.
Protocol here (preregistered, contamination-safe):
  - TRAIN split: first 40 training tasks, used to EVOLVE prompts
  - TEST split:  40 held-out evaluation tasks, used ONLY to verify
  - a prompt's official score = accuracy on TEST, never used for selection

Selection on train, report on test = no overfitting to the benchmark.
"""
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/omega/.hermes/iqbench")
from iq_bench import load_endpoints  # endpoint/key resolution (ollama-cloud etc.)

ARC_DIR = Path("/home/omega/.hermes/iqbench/arc_data")
TRAIN_N = 40   # evolve on these
TEST_N = 40    # verify on these (never used for selection)


def install_corpus():
    """Clone fchollet/ARC into arc_data/ if needed."""
    import subprocess
    if (ARC_DIR / "data" / "training").exists():
        return
    ARC_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1",
                    "https://github.com/fchollet/ARC.git", str(ARC_DIR)],
                   check=True, capture_output=True)


def load_tasks():
    install_corpus()
    train = {}
    for f in sorted((ARC_DIR / "data" / "training").glob("*.json")):
        train[f.stem] = json.loads(f.read_text())
    evalr = {}
    for f in sorted((ARC_DIR / "data" / "evaluation").glob("*.json")):
        evalr[f.stem] = json.loads(f.read_text())
    return train, evalr


def grid_to_text(g):
    return "\n".join("".join(str(c) for c in row) for row in g)


def task_to_prompt(task, test_index=0):
    """Render one ARC task as text (all pairs, then the test input)."""
    parts = ["GRID PUZZLE. Digits are colors. Find the transformation rule from the"
             "input→output example pairs, then apply it to the test input."]
    for i, pair in enumerate(task["train"]):
        parts.append(f"--- example {i+1} ---")
        parts.append(f"input:\n{grid_to_text(pair['input'])}")
        parts.append(f"output:\n{grid_to_text(pair['output'])}")
    parts.append(f"--- TEST input ---\n{grid_to_text(task['test'][test_index]['input'])}")
    parts.append("Reply with ONLY the output grid: one row per line, digits "
                 "separated by spaces, no other text.")
    return "\n".join(parts)


def parse_grid(out):
    """Strict content-only grid; no prose or answer-key-assisted selection."""
    from research_core import strict_parse_grid
    return strict_parse_grid(out)[0]


def grids_equal(a, b):
    return a == b and bool(a)


def run_task(model, url, key, task, sysprompt, max_tokens=16000, test_index=0):
    """One test case. Parse content only, and only with finish_reason=stop."""
    import httpx
    msgs = []
    if sysprompt:
        msgs.append({"role": "system", "content": sysprompt})
    msgs.append({"role": "user", "content": task_to_prompt(task, test_index)})
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {key}"}, json={
            "model": model, "temperature": 0.2, "max_tokens": max_tokens,
            "messages": msgs}, timeout=600)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        out = msg.get("content") or ""
        finish = (data.get("choices") or [{}])[0].get("finish_reason")
        return (parse_grid(out) if finish == "stop" else None), out
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:80]}"


def score_task(model, url, key, task, sysprompt, task_id, live=None):
    outcomes = []
    for index, case in enumerate(task["test"]):
        got, raw = run_task(model, url, key, task, sysprompt, test_index=index)
        ok = grids_equal(got, case["output"])
        outcomes.append(ok)
        if live:
            live.write(json.dumps({"task": task_id, "test_index": index,
                                   "correct": ok, "got": got, "raw": raw}) + "\n")
            live.flush()
    return bool(outcomes) and all(outcomes)
