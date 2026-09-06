"""Cluster A — strict FINAL parser, ARC strict grid parser, split & stats (RED first)."""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "/home/omega/.hermes/iqbench")

from research_core import (
    parse_final_strict,
    strict_parse_grid,
    split_items,
    wilson_ci,
    mcnemar_exact,
)


# ── strict FINAL parser ────────────────────────────────────────────────────

def test_final_signed_negative():
    r = parse_final_strict("blah\nFINAL: -3")
    assert r.value == "-3" and r.status == "ok" and r.kind == "numeric"

def test_final_positive_and_float():
    assert parse_final_strict("FINAL: 42").value == "42"
    r = parse_final_strict("FINAL: -2.5")
    assert r.value == "-2.5" and r.kind == "numeric"

def test_final_option_number():
    r = parse_final_strict("FINAL: option 4")
    assert r.value == "4" and r.kind == "option"

def test_final_valid_invalid():
    assert parse_final_strict("FINAL: VALID").value == "VALID"
    assert parse_final_strict("FINAL: invalid").value == "INVALID"
    assert parse_final_strict("FINAL: INVALID.").value == "INVALID"

def test_no_final_marker():
    r = parse_final_strict("the answer is 5 I think")
    assert r.status == "no_final" and r.value is None

def test_tentative_mentions_do_not_count():
    # reasoning mentioning FINAL-like answers without the marker is NOT final
    r = parse_final_strict("maybe option 3? Let me think.\nFINAL: option 2")
    assert r.value == "2"

def test_multiple_identical_finals_ok():
    r = parse_final_strict("FINAL: 7\nsome text\nFINAL: 7")
    assert r.status == "ok" and r.value == "7"

def test_multiple_differing_finals_fail_closed():
    r = parse_final_strict("FINAL: 7\nFINAL: option 3")
    assert r.status == "ambiguous" and r.value is None

def test_extra_words_unparseable():
    r = parse_final_strict("FINAL: the answer is forty two")
    assert r.status == "unparseable" and r.value is None

def test_trailing_punctuation_tolerated_but_no_prose():
    assert parse_final_strict("FINAL: 12.").value == "12"
    assert parse_final_strict("FINAL: 12 apples").status == "unparseable"

def test_empty():
    r = parse_final_strict("")
    assert r.status == "no_final"

def test_finish_reason_length_overrides():
    r = parse_final_strict("FINAL: 5", finish_reason="length")
    assert r.status == "truncated" and r.value is None

def test_no_answer_key_dependency(parse=None):
    # parser must accept an optional expected arg and NEVER use it
    r = parse_final_strict("FINAL: 9", expected="3")
    assert r.value == "9"


# ── ARC strict grid parser ─────────────────────────────────────────────────

def test_grid_basic():
    g, why = strict_parse_grid("1 2 3\n4 5 6")
    assert g == [[1, 2, 3], [4, 5, 6]] and why == "ok"

def test_grid_1x1_allowed():
    g, why = strict_parse_grid("7")
    assert g == [[7]] and why == "ok"

def test_grid_contiguous_digits_no_spaces():
    # canonical ARC style: rows of contiguous digits are valid too
    g, why = strict_parse_grid("123\n456")
    assert g == [[1, 2, 3], [4, 5, 6]]

def test_grid_prose_rejected():
    g, why = strict_parse_grid("The grid is:\n1 2 3\nrow two: 4 5 6")
    # "row two:" line is not a digit row → ambiguous/invalid, must NOT harvest
    assert g is None and why in ("ambiguous", "invalid")

def test_grid_multidigit_token_rejected():
    g, why = strict_parse_grid("1 12 3\n4 5 6")
    assert g is None and why == "invalid"

def test_grid_value_range_rejected():
    g, why = strict_parse_grid("1 2\n3 42")
    assert g is None

def test_grid_ragged_rejected():
    g, why = strict_parse_grid("1 2 3\n4 5")
    assert g is None

def test_grid_max_30():
    row = " ".join(["1"] * 31)
    assert strict_parse_grid(row)[0] is None
    row30 = " ".join(["1"] * 30)
    assert strict_parse_grid(row30)[0] == [[1] * 30]

def test_grid_empty():
    assert strict_parse_grid("")[0] is None
    assert strict_parse_grid("no numbers here at all")[0] is None

def test_grid_explanation_prose_digits_not_harvested():
    # digits embedded in prose must never become rows
    g, why = strict_parse_grid("Step 1: add 2 to everything.\n0 0\n0 0")
    assert g is None and why == "invalid"


# ── split ──────────────────────────────────────────────────────────────────

def _items(n):
    return [{"question": f"question number {i} unique text", "answer": str(i),
             "family": "num", "difficulty": 100} for i in range(n)]

def test_split_disjoint_no_cross_duplicates():
    items = _items(40)
    dev, hold = split_items(items, dev_n=10, holdout_n=10, seed=1)
    assert len(dev) == 10 and len(hold) == 10
    dh = {i["question"] for i in dev}
    hh = {i["question"] for i in hold}
    assert not (dh & hh)

def test_split_deterministic_cross_process_like():
    items = _items(40)
    a = split_items(items, 10, 10, seed=7)
    b = split_items(items, 10, 10, seed=7)
    assert [i["question"] for i in a[0]] == [i["question"] for i in b[0]]

def test_split_dedups_duplicate_questions():
    items = _items(10) + _items(10)  # exact duplicates
    with pytest.raises(ValueError, match="insufficient"):
        split_items(items, 8, 8, seed=0)


def test_final_marker_must_be_final_line_not_quoted_reasoning():
    assert parse_final_strict('I considered "FINAL: 3" but am unsure').value is None
    assert parse_final_strict('FINAL: 3\nActually I am unsure').value is None
    assert parse_final_strict('FINAL: +5').value == '5'


def test_raven_seed_and_rederived_rows():
    import random
    from iq_bench import build_test, gen_raven
    assert build_test(seed=0) == build_test(seed=0)
    for seed in range(100):
        item = gen_raven(random.Random(seed), 1)
        rows = item['question'].split('\n\n')[1].splitlines()
        a, b = [[int(x) for x in row.split()] for row in rows[:2]]
        c = [int(x) for x in rows[2].split()[:2]]
        assert a[1]-a[0] == a[2]-a[1] == b[1]-b[0] == b[2]-b[1] == c[1]-c[0]
        assert int(item['_ansval']) == 2*c[1]-c[0]


# ── stats ──────────────────────────────────────────────────────────────────

def test_wilson_bounds():
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0 and 0 <= hi < 0.35
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0 and lo > 0.65

def test_mcnemar_exact_significant():
    # 8 only-champion vs 0 only-baseline → strong evidence
    p = mcnemar_exact(b=8, c=0)
    assert p < 0.01

def test_mcnemar_exact_null():
    p = mcnemar_exact(b=5, c=5)
    assert p == 1.0

def test_bootstrap_deterministic():
    from research_core import paired_bootstrap_delta
    pairs = [(True, False)] * 6 + [(True, True)] * 4 + [(False, False)] * 10
    d1 = paired_bootstrap_delta(pairs, n=1000, seed=42)
    d2 = paired_bootstrap_delta(pairs, n=1000, seed=42)
    assert d1 == d2
    assert d1["mean"] > 0
