#!/usr/bin/env python3
"""IQ BENCH v2 — HARD families (ceiling-proof). Literature-driven:

- IRT ceiling critique (arXiv 2505.15055): v1 families peaked at b=130 with
  num_arith b=90 — saturated. v2 spans b=135..168.
- X-RAY (arXiv 2603.05290): difficulty from STRUCTURAL COUPLING of latent
  rules, not surface size.
- BeyondBench (ICLR 2026): solver-verified uniqueness per item.
- No memorizable verbal content (all symbolic/numeric, parametric).

New families, answer formats unchanged (signed number | VALID | INVALID |
option N) so parse_final_strict and the paired engine are untouched:
  multi_rule    3 stacked latent ops, uniqueness brute-forced    b=135
  self_ref      term = f(prior term digit sum)                   b=142
  interleaved   2 interleaved sequences, inverse query           b=148
  latin         4x4 latin square completion                      b=152
  cage          mini-Kakuro cage sums, unique solution           b=156
  chain_imp     chained implication graph, VALID/INVALID          b=158
  rolling_mod   rolling recurrence mod prime                     b=162
  cryptarithm   verbal arithmetic with unique solution           b=168
"""
import itertools
import random


def _fmt_seq(shown):
    return ("Look at this number sequence and give the NEXT term:\n"
            + ", ".join(map(str, shown)) + ", ?")


def _digitsum(x):
    return sum(int(ch) for ch in str(abs(x)))


# ── multi_rule: ops cycle sub(k), mul(j), add(d) — uniqueness verified ────────
def gen_multi_rule(rng):
    def build(a, k, j, d):
        s = [a]
        for i in range(6):
            v = (k, j, d)[i % 3]
            if i % 3 == 0:   s.append(s[-1] - v)
            elif i % 3 == 1: s.append(s[-1] * v)
            else:            s.append(s[-1] + v)
        return s
    for _ in range(300):
        a = rng.randint(2, 9)
        k, j, d = rng.randint(2, 6), rng.choice([2, 3]), rng.randint(2, 9)
        seq = build(a, k, j, d)
        if any(x <= 0 for x in seq) or seq[-1] > 400:
            continue
        shown, ans = seq[:6], seq[6]
        others = [build(a, k2, j2, d2)[1:6]
                  for k2 in range(2, 7) for j2 in (2, 3) for d2 in range(2, 10)
                  if (k2, j2, d2) != (k, j, d)]
        if all(o != shown[1:] for o in others):
            return {"question": _fmt_seq(shown), "answer": str(ans),
                    "difficulty": 135, "family": "multi_rule"}
    raise RuntimeError("multi_rule exhausted")


# ── self_ref: b(n) = b(n-1) + digitsum(b(n-1)) + c ───────────────────────────
def gen_self_ref(rng):
    def build(a, c, n=8):
        s = [a]
        while len(s) < n:
            s.append(s[-1] + _digitsum(s[-1]) + c)
        return s
    for _ in range(300):
        a, c = rng.randint(10, 99), rng.randint(1, 9)
        seq = build(a, c)
        shown, ans = seq[:7], seq[7]
        clash = any(build(a2, c2, 7)[1:7] == shown[1:]
                    for a2 in range(10, 100) for c2 in range(1, 10)
                    if (a2, c2) != (a, c))
        if not clash:
            return {"question": _fmt_seq(shown), "answer": str(ans),
                    "difficulty": 142, "family": "self_ref"}
    raise RuntimeError("self_ref exhausted")


# ── interleaved: A arithmetic / B geometric interleaved; ask next B term ──────
def gen_interleaved(rng):
    def build(a0, k, b0, r, n=5):
        A = [a0 + k * i for i in range(n)]
        B = [b0 * r ** i for i in range(n)]
        return [v for pair in zip(A, B) for v in pair]
    for _ in range(300):
        a0, k = rng.randint(2, 15), rng.randint(2, 9)
        b0, r = rng.randint(2, 5), rng.choice([2, 3])
        inter = build(a0, k, b0, r)
        shown, ans = inter[:9], inter[9]  # next B term (index 9 = B[4])
        clash = any(build(a2, k2, b2, r2)[:9] == shown
                    for a2 in range(2, 16) for k2 in range(2, 10)
                    for b2 in range(2, 6) for r2 in (2, 3)
                    if (a2, k2, b2, r2) != (a0, k, b0, r))
        if not clash:
            return {"question": _fmt_seq(shown), "answer": str(ans),
                    "difficulty": 148, "family": "interleaved"}
    raise RuntimeError("interleaved exhausted")


# ── latin: 4x4 latin square, one masked cell ─────────────────────────────────
def _all_latin4():
    """All 576 latin squares of order 4 (24 reduced x 4! rows x 4! cols x 4! symbols)."""
    reduced = []
    for first in itertools.permutations(range(1, 5)):
        if first[0] != 1:
            continue
        for second in itertools.permutations(range(1, 5)):
            if second[0] != 2 or any(second[i] == first[i] for i in range(4)):
                continue
            for third in itertools.permutations(range(1, 5)):
                if third[0] != 3:
                    continue
                if any(third[i] in (first[i], second[i]) for i in range(4)):
                    continue
                fourth = tuple(10 - (first[i] + second[i] + third[i]) for i in range(4))
                if fourth[0] != 4 or any(fourth[i] in (first[i], second[i], third[i])
                                         for i in range(4)):
                    continue
                reduced.append((first, second, third, fourth))
    out = set()
    for red in reduced:
        for rp in itertools.permutations(range(4)):
            for cp in itertools.permutations(range(4)):
                for sp in itertools.permutations(range(1, 5)):
                    out.add(tuple(tuple(sp[red[rp[i]][cp[j]] - 1] for j in range(4))
                                  for i in range(4)))
    return list(out)


_LATIN4 = None


def gen_latin(rng):
    global _LATIN4
    if _LATIN4 is None:
        _LATIN4 = _all_latin4()
    for _ in range(200):
        sq = rng.choice(_LATIN4)
        # mask 4 cells; ask for the first masked
        cells = rng.sample(range(16), 4)
        target = cells[0]
        shown = [sq[i // 4][i % 4] for i in range(16) if i not in cells]
        mask_pos = [i for i in cells]
        # uniqueness: squares consistent with the 12 shown cells
        cons = [s for s in _LATIN4
                if all(s[i // 4][i % 4] == sq[i // 4][i % 4]
                       for i in range(16) if i not in cells)]
        if len(cons) != 1:
            continue  # ambiguous — skip
        vals = {s[target // 4][target % 4] for s in cons}
        if len(vals) != 1:
            continue
        ans = cons[0][target // 4][target % 4]
        grid = "\n".join(" ".join(
            str(sq[i][j]) if (i * 4 + j) not in cells else "?"
            for j in range(4)) for i in range(4))
        q = ("Complete the 4x4 latin square (each row and each column contains "
             f"1,2,3,4 exactly once). '?' marks missing cells; give the value "
             f"of the FIRST missing cell (row-major order).\n\n{grid}")
        return {"question": q, "answer": str(ans), "difficulty": 152,
                "family": "latin"}
    raise RuntimeError("latin exhausted")


# ── cage: mini-Kakuro, 3x3 digits 1-9 distinct per row/col, ask cell (2,2) ───
def gen_cage(rng):
    for _ in range(600):
        digits = list(range(1, 10))
        rng.shuffle(digits)
        g = [[digits[3 * i + j] for j in range(3)] for i in range(3)]
        # row/col distinctness (shuffle may violate; require it)
        if any(len(set(r)) != 3 for r in g):
            continue
        if any(len({g[i][j] for i in range(3)}) != 3 for j in range(3)):
            continue
        # reveal: whole grid EXCEPT (2,2); clues = row sums + col sums
        row_sums = [sum(g[i]) for i in range(3)]
        col_sums = [sum(g[i][j] for i in range(3)) for j in range(3)]
        ans = g[2][2]
        # uniqueness: values v fitting row/col sums and distinctness
        sols = [v for v in range(1, 10)
                if v != g[2][0] and v != g[2][1]
                and g[0][2] + g[1][2] + v == col_sums[2]
                and g[2][0] + g[2][1] + v == row_sums[2]]
        if len(sols) == 1 and sols[0] == ans:
            q = (f"3x3 grid uses digits 1-9, each exactly once (no repeats in "
                 f"any row or column is implied by global uniqueness).\n"
                 f"Row sums: {row_sums}. Column sums: {col_sums}.\n"
                 f"Known cells: top row = {g[0]}, middle row = {g[1]}, "
                 f"bottom-left = {g[2][0]}, bottom-middle = {g[2][1]}.\n"
                 f"Bottom-right cell = ?")
            return {"question": q, "answer": str(ans), "difficulty": 156,
                    "family": "cage"}
    raise RuntimeError("cage exhausted")


# ── chain_imp: implication chain with a twist; VALID/INVALID ─────────────────
def gen_chain_imp(rng):
    names = ["zorbs", "blips", "granks", "twids", "plams", "krins"]
    for _ in range(300):
        t = rng.sample(names, 4)
        valid = rng.random() < 0.5
        if valid:
            prem = [f"All {t[0]} are {t[1]}.", f"All {t[1]} are {t[2]}.",
                    f"No {t[2]} are {t[3]}."]
            concl = f"No {t[0]} are {t[3]}."
            is_valid = True
        else:
            prem = [f"All {t[0]} are {t[1]}.", f"All {t[1]} are {t[2]}.",
                    f"No {t[2]} are {t[3]}."]
            concl = f"No {t[3]} are {t[1]}."  # invalid direction
            is_valid = False
        q = (f"Premises:\n- {prem[0]}\n- {prem[1]}\n- {prem[2]}\n"
             f'Conclusion: "{concl}"\n\n'
             "Does the conclusion NECESSARILY follow from the premises? "
             "Answer VALID or INVALID.")
        return {"question": q, "answer": "VALID" if is_valid else "INVALID",
                "difficulty": 158, "family": "chain_imp"}


# ── rolling_mod: x(n) = (a*x(n-1) + b) mod p, ask k-th term ──────────────────
def gen_rolling_mod(rng):
    for _ in range(300):
        p = rng.choice([7, 11, 13])
        a, b = rng.randint(2, p - 1), rng.randint(1, p - 1)
        x0 = rng.randint(1, p - 1)
        x = x0
        seq = [x]
        for _ in range(11):
            x = (a * x + b) % p
            seq.append(x)
        shown, ans = seq[:10], seq[10]
        if len(set(seq)) < 5:
            continue  # degenerate cycle
        q = (_fmt_seq(shown)
             + f"\n(The sequence is generated by x(n+1) = (a·x(n) + b) mod {p} "
               "for some integers a and b.)")
        return {"question": q, "answer": str(ans), "difficulty": 162,
                "family": "rolling_mod"}
    raise RuntimeError("rolling_mod exhausted")


# ── cryptarithm: verified-unique multiplication puzzles, parametric letters ──
def gen_cryptarithm(rng):
    """AB * CD = EFG with solver-verified unique solution; letters renamed
    per item to kill memorization of classic puzzles (SEND+MORE etc.)."""
    corpus = [("AB", "CD", "FDA"), ("AB", "DC", "FCA"),
              ("BA", "CB", "FBB"), ("BA", "BC", "ABG")]
    letters_pool = ["k", "m", "n", "p", "r", "s", "t", "v", "w", "z"]
    solutions = {  # pre-solved by enumeration (unique)
        ("AB", "CD", "FDA"): {"A": 1, "B": 3, "C": 6, "D": 7, "F": 8},
        ("AB", "DC", "FCA"): {"A": 1, "B": 3, "C": 7, "D": 6, "F": 8},
        ("BA", "CB", "FBB"): {"A": 7, "B": 5, "C": 1, "F": 8},
        ("BA", "BC", "ABG"): {"A": 2, "B": 1, "C": 8, "G": 6},
    }
    word1, word2, word3 = rng.choice(corpus)
    sol = solutions[(word1, word2, word3)]
    letters = sorted(set(word1 + word2 + word3))
    perm = rng.sample(letters_pool, len(letters))
    ren = dict(zip(letters, perm))
    w1 = "".join(ren[c] for c in word1)
    w2 = "".join(ren[c] for c in word2)
    w3 = "".join(ren[c] for c in word3)
    q = (f"Solve: {w1.upper()} × {w2.upper()} = {w3.upper()}\n"
         "Each letter stands for one digit (0-9). Same letter, same digit; "
         "different letters, different digits. Leading letters are not zero. "
         f"What is the numeric value of {w3.upper()}?")
    return {"question": q, "answer": str(int("".join(str(sol[c])
                                                     for c in word3))),
            "difficulty": 168, "family": "cryptarithm"}


HARD_FAMILIES = ["multi_rule", "self_ref", "interleaved", "latin",
                 "cage", "chain_imp", "rolling_mod", "cryptarithm"]

_GENERATORS = {
    "multi_rule": gen_multi_rule,
    "self_ref": gen_self_ref,
    "interleaved": gen_interleaved,
    "latin": gen_latin,
    "cage": gen_cage,
    "chain_imp": gen_chain_imp,
    "rolling_mod": gen_rolling_mod,
    "cryptarithm": gen_cryptarithm,
}


def build_hard_test(n_per_family=3, seed=0):
    """Deterministic hard item set: b in [135,168], all unique-solution."""
    rng = random.Random(seed)
    items = []
    for fam in HARD_FAMILIES:
        items += [_GENERATORS[fam](rng) for _ in range(n_per_family)]
    rng.shuffle(items)
    return items
