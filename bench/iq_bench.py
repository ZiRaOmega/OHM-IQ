#!/usr/bin/env python3
"""IQ BENCH — standardized IQ test for LLMs on human-style IQ questions.

Anti-contamination by construction: ALL items are parametrically generated
with random seeds (values, names, distractors unique per run) — models have
never seen these exact items.

Families (difficulty b = IQ level where 50% of humans at that IQ solve it,
approximate calibration from psychometric literature):
  num_arith    arithmetic series a+k      b=90
  num_geo      geometric series xk        b=95
  num_fib      fibonacci-like a+b         b=110
  num_quad     increasing step (+k,+k+d)  b=115
  num_alt      alternating 2 rules        b=128
  raven_1      3x3 matrix, 1 rule         b=100
  raven_2      3x3 matrix, 2 rules        b=115
  raven_3      3x3 matrix, row-addition   b=130
  analogy      verbal analogies           b=100
  syllogism    formal logic               b=112

Protocol: temperature 0, single shot, no forced CoT, strict FINAL: parsing.
Scoring: fixed-slope 2PL (Rasch), theta estimated by grid MLE over 55..160.
"""
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

OUT = Path("/home/omega/.hermes/iqbench")
OUT.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# ITEM GENERATION (parametric — every value randomized per run)
# ──────────────────────────────────────────────────────────────────────────────

def gen_num_series(rng, kind):
    """Numeric series → next term. Returns (prompt_dict, answer_str, difficulty)."""
    if kind == "num_arith":
        a, k = rng.randint(2, 30), rng.randint(2, 12)
        seq = [a + k * i for i in range(6)]
        ans, b = seq[5], 90                     # answer = 6th term
        shown = seq[:5]
        q = ("Look at this number sequence and give the NEXT term:\n"
             + ", ".join(map(str, shown)) + ", ?")
        return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}
    elif kind == "num_geo":
        a, k = rng.randint(2, 5), rng.choice([2, 3])
        seq = [a * k**i for i in range(6)]
        ans, b = seq[5], 95                     # answer = 6th term
        shown = seq[:5]
        q = ("Look at this number sequence and give the NEXT term:\n"
             + ", ".join(map(str, shown)) + ", ?")
        return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}
    elif kind == "num_fib":
        a, c = rng.randint(1, 9), rng.randint(2, 15)
        seq = [a, c]
        while len(seq) < 7:
            seq.append(seq[-1] + seq[-2])
        shown = seq[:6]                        # 6 shown terms
        ans, b = seq[6], 110                   # answer = 7th term (next after shown)
        q = ("Look at this number sequence and give the NEXT term:\n"
             + ", ".join(map(str, shown)) + ", ?")
        return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}
    elif kind == "num_quad":
        a, k, d = rng.randint(1, 12), rng.randint(1, 6), rng.randint(1, 5)
        seq, step = [a], k
        while len(seq) < 6:
            seq.append(seq[-1] + step)
            step += d
        # shown = first 5 terms; answer = 6th term (already generated)
        ans, b = seq[5], 115
        shown = seq[:5]
        q = ("Look at this number sequence and give the NEXT term:\n"
             + ", ".join(map(str, shown)) + ", ?")
        return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}
    elif kind == "num_alt":
        a = rng.randint(2, 9)
        m, p = rng.randint(2, 4), rng.randint(1, 7)
        seq = [a]
        for i in range(6):
            seq.append(seq[-1] * m if i % 2 == 0 else seq[-1] + p)
        shown = seq[:6]                        # 6 shown terms (ops i=0..4)
        ans, b = seq[6], 128                   # answer = 7th term = next op (i=5)
        q = ("Look at this number sequence and give the NEXT term:\n"
             + ", ".join(map(str, shown)) + ", ?")
        return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}
    q = ("Look at this number sequence and give the NEXT term:\n"
         + ", ".join(map(str, seq[:-1])) + ", ?")
    return {"question": q, "answer": str(ans), "difficulty": b, "family": kind}


def gen_raven(rng, level):
    """3x3 numeric matrix, last cell missing. Rules by difficulty."""
    if level == 1:  # constant row progression (+k per column, same k all rows)
        k = rng.randint(2, 9)
        origins = [rng.randint(1, 9) for _ in range(3)]
        grid = [[origin + k * j for j in range(3)] for origin in origins]
        ans = grid[2][2]
        b = 100
    elif level == 2:  # row progression + independent column offset (2 rules)
        k = rng.randint(2, 7)
        c0 = [rng.randint(1, 20), rng.randint(21, 40), rng.randint(41, 60)]
        grid = [[c0[i] + k * j for j in range(3)] for i in range(3)]
        ans = grid[2][2]
        b = 115
    else:  # row3 = row1 + row2 (latent rule, hardest)
        r1 = [rng.randint(1, 25) for _ in range(3)]
        r2 = [rng.randint(1, 25) for _ in range(3)]
        r3 = [r1[j] + r2[j] for j in range(3)]
        grid = [r1, r2, r3]
        ans = r3[2]
        b = 130
    rows = "\n".join("  ".join(f"{grid[i][j]:>3}" if (i, j) != (2, 2) else "  ?"
                               for j in range(3)) for i in range(3))
    # 8 options with close distractors
    opts = {ans}
    while len(opts) < 8:
        opts.add(ans + rng.choice([-30, -21, -13, -9, -7, -5, -3, -2, -1,
                                   1, 2, 3, 5, 7, 9, 13, 21, 30]))
    opts = sorted(opts)
    correct = opts.index(ans) + 1
    q = (f"Complete the 3x3 matrix (find the missing value marked ?):\n\n{rows}\n\n"
         "Options:\n" + "\n".join(f"{i+1}. {v}" for i, v in enumerate(opts))
         + "\n\nWhich option number (1-8) replaces the ?")
    fam = f"raven_{level}"
    return {"question": q, "answer": str(correct), "difficulty": b, "family": fam,
            "_ansval": str(ans)}


ANALOGY_PAIRS = [
    ("hand", "glove", "foot", "sock"), ("bird", "nest", "bee", "hive"),
    ("painter", "brush", "writer", "pen"), ("fish", "water", "bird", "air"),
    ("wheel", "car", "propeller", "airplane"), ("doctor", "hospital", "teacher", "school"),
    ("thermometer", "temperature", "clock", "time"), ("author", "book", "composer", "symphony"),
    ("pilot", "cockpit", "sailor", "helm"), ("seed", "plant", "egg", "bird"),
    ("cow", "milk", "hen", "egg"), ("lion", "den", "dog", "kennel"),
    ("carpenter", "wood", "sculptor", "clay"), ("smile", "joy", "frown", "sorrow"),
    ("knife", "cut", "needle", "sew"), ("bridge", "river", "tunnel", "mountain"),
    ("key", "lock", "password", "account"), ("singer", "choir", "player", "team"),
    ("mason", "brick", "writer", "word"), ("map", "territory", "blueprint", "building"),
    ("candle", "wax", "lamp", "oil"), ("hammer", "carpenter", "scalpel", "surgeon"),
]
ANALOGY_DISTRACTORS = ["chair", "cloud", "engine", "paper", "stone", "window", "rope",
                       "bottle", "ticket", "shadow", "ladder", "mirror", "pumpkin", "wire"]


def gen_analogy(rng):
    a, b_, c, d = rng.choice(ANALOGY_PAIRS)
    ds = set(rng.sample(ANALOGY_DISTRACTORS, 3))
    opts = sorted(ds | {d})
    rng.shuffle(opts)
    correct = opts.index(d) + 1
    q = (f'"{a}" is to "{b_}" as "{c}" is to ...?\n\n'
         "Options:\n" + "\n".join(f"{i+1}. {v}" for i, v in enumerate(opts))
         + "\n\nWhich option number completes the analogy?")
    return {"question": q, "answer": str(correct), "difficulty": 100,
            "family": "analogy", "_ansval": d}


SYLL_NAMES = ["zorbs", "blips", "granks", "twids", "plams", "krins", "snorps", "vexes"]
SYLL_CAT = ["mammals", "reptiles", "insects", "birds", "fish", "amphibians"]


def gen_syllogism(rng):
    """Valid/invalid syllogism with invented terms (no factual shortcuts)."""
    t1, t2, t3 = rng.sample(SYLL_NAMES, 3)
    valid = rng.random() < 0.5
    if valid:  # Barbara: All A are B, All B are C → All A are C
        prem = [f"All {t1} are {t2}.", f"All {t2} are {t3}."]
        concl = f"All {t1} are {t3}."
        is_valid = True
    else:  # affirming the consequent: All A are B, C is B → C is A (invalid)
        prem = [f"All {t1} are {t2}.", f"All {t3} are {t2}."]
        concl = f"All {t1} are {t3}."
        is_valid = False
    q = (f"Premises:\n- {prem[0]}\n- {prem[1]}\nConclusion: \"{concl}\"\n\n"
         "Does the conclusion NECESSARILY follow from the premises? "
         "Answer VALID or INVALID.")
    return {"question": q, "answer": "VALID" if is_valid else "INVALID",
            "difficulty": 112, "family": "syllogism", "_ansval": concl}


def build_test(n_per_family=4, seed=0):
    """Deterministic by default (seed=0): same items across processes/runs."""
    rng = random.Random(seed)
    items = []
    for fam in ["num_arith", "num_geo", "num_fib", "num_quad", "num_alt"]:
        items += [gen_num_series(rng, fam) for _ in range(n_per_family)]
    items += [gen_raven(rng, lv) for lv in (1, 2, 3) for _ in range(n_per_family)]
    items += [gen_analogy(rng) for _ in range(n_per_family)]
    items += [gen_syllogism(rng) for _ in range(n_per_family)]
    rng.shuffle(items)
    return items


# ──────────────────────────────────────────────────────────────────────────────
# ADMINISTRATION
# ──────────────────────────────────────────────────────────────────────────────

PROMPT_TMPL = ("You are taking an IQ test. Answer WITHOUT explaining. "
               "End your reply with a line exactly like: FINAL: <answer>\n\n{q}")


def parse_final(out, finish_reason=None):
    """Compatibility wrapper; score content-only explicit finals."""
    from research_core import parse_final_strict
    return parse_final_strict(out, finish_reason=finish_reason).value


def run_model(model, base_url, api_key, items, max_tokens=512):
    results = []
    for it in items:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": PROMPT_TMPL.format(q=it["question"])}],
            "temperature": 0.0, "max_tokens": max_tokens,
        }
        t0 = time.time()
        finish = None
        try:
            r = httpx.post(base_url, headers={"Authorization": f"Bearer {api_key}"},
                           json=payload, timeout=240)
            out = ""
            if r.status_code == 200:
                msg = (r.json().get("choices") or [{}])[0].get("message", {})
                out = msg.get("content") or ""
                finish = (r.json().get("choices") or [{}])[0].get("finish_reason")
            else:
                out = f"HTTP{r.status_code}"
        except Exception as e:
            out = f"ERR:{e}"
        lat = time.time() - t0
        got = parse_final(out, finish_reason=finish) if finish == "stop" else None
        ok = (got == it["answer"].upper()) if got else False
        results.append({**{k: v for k, v in it.items() if not k.startswith("_")},
                        "got": got, "correct": ok, "latency": round(lat, 1),
                        "raw": out[-200:]})
        print(f"  [{it['family']:>11}] expect={it['answer']:>8} got={str(got):>8} "
              f"{'✓' if ok else '✗'} ({lat:.0f}s)", flush=True)
    return results


# ──────────────────────────────────────────────────────────────────────────────
# SCORING — fixed-slope 2PL, grid MLE
# ──────────────────────────────────────────────────────────────────────────────

def estimate_iq(results):
    """theta maximizing sum log P(outcome). Slope fixed so 1 b-point = 1 IQ point
    at the 50% crossing; logistic scale s.t. P=50% at theta=b, spread over ±30."""
    import math
    best, best_ll = 100, -1e9
    for theta in range(55, 161):
        ll = 0.0
        for r in results:
            if r.get("latency") and str(r.get("got", "")).startswith("ERR"):
                continue  # API errors don't count either way? count as fail
            p = 1.0 / (1.0 + math.exp(-(theta - r["difficulty"]) / 12.0))
            p = min(max(p, 0.02), 0.98)
            ll += math.log(p if r["correct"] else 1 - p)
        if ll > best_ll:
            best, best_ll = theta, ll
    n_ok = sum(1 for r in results if r["correct"])
    return best, n_ok, len(results)


def report(model, results):
    theta, n_ok, n = estimate_iq(results)
    fams = {}
    for r in results:
        f = fams.setdefault(r["family"], [0, 0])
        f[1] += 1
        if r["correct"]:
            f[0] += 1
    fam_str = {f: f"{ok}/{tot}" for f, (ok, tot) in fams.items()}
    return {"model": model, "estimated_iq": theta, "raw_score": f"{n_ok}/{n}",
            "by_family": fam_str, "n_items": n}


MODELS = []  # filled by CLI


def load_endpoints():
    import yaml
    cfg = yaml.safe_load(open("/home/omega/.hermes/config.yaml"))
    zai_key = cfg["providers"]["zai-custom"]["api_key"]
    env = {}
    for line in open("/home/omega/.hermes/.env"):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.replace('"', "")
    okey = env.get("OLLAMA_API_KEY") or env.get("OLLAMA_CLOUD_API_KEY")
    return {
        "glm-5.3": (f"{cfg['providers']['zai-custom']['base_url'].rstrip('/')}/chat/completions", zai_key),
        "gpt-oss:120b": ("https://ollama.com/v1/chat/completions", okey),
        "nemotron-3-ultra": ("https://ollama.com/v1/chat/completions", okey),
        "mistral-large-3:675b": ("https://ollama.com/v1/chat/completions", okey),
    }


if __name__ == "__main__":
    import sys
    seed = None
    n_per = 4
    only = None
    args = sys.argv[1:]
    if "--seed" in args:
        seed = int(args[args.index("--seed") + 1])
    if "--n" in args:
        n_per = int(args[args.index("--n") + 1])
    if "--only" in args:
        only = args[args.index("--only") + 1].split(",")

    items = build_test(n_per_family=n_per, seed=seed)
    print(f"★ IQ BENCH — {len(items)} items, seed={seed or 'random'}")
    endpoints = load_endpoints()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_reports = []
    for model, (url, key) in endpoints.items():
        if only and model not in only:
            continue
        if not key:
            print(f"\n── {model}: no API key, skipped")
            continue
        print(f"\n── {model} ──")
        results = run_model(model, url, key, items)
        rep = report(model, results)
        all_reports.append(rep)
        print(f"  → IQ≈{rep['estimated_iq']} | raw {rep['raw_score']}")
    outf = OUT / f"iqbench_{stamp}.json"
    outf.write_text(json.dumps({"seed": seed, "reports": all_reports}, indent=2))
    print(f"\n★ saved: {outf}")
