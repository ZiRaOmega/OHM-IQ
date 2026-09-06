# OHM-IQ — Do Real Production System Prompts Do Anything?

> **145 leaked production system prompts. One deterministic benchmark. Paired McNemar tests. The question nobody has asked with the data nobody has tested.**

Everyone collects leaked system prompts. Nobody measures them.

The repos below hold thousands of leaked production prompts — Cursor's agent, Devin's session boot, Manus's full persona, o1's reasoning charter. Meanwhile, the academic literature has thoroughly studied **synthetic** personas ("You are a brilliant mathematician") and found, repeatedly, that they do ~nothing ([Zheng et al. 2023](https://arxiv.org/abs/2311.10054): 162 personas, no gains; [Wharton GAIl 2025](https://gail.wharton.upenn.edu/research-and-insights/playing-pretend-expert-personas/): no accuracy lift; [Principled Personas, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.1364/): volatility, not expertise).

**Nobody has crossed the two streams.** Do the *actual* production personas — battle-tested instruction sets written by frontier labs, not toy roles — measurably change reasoning performance when transplanted onto another model? That is a genuinely open empirical question, and this repo is the instrument built to answer it.

## What this is

A measurement engine, not a prompt collection:

- **145 production prompts** (57 vendors: OpenAI, Anthropic, xAI, Cursor, Devin, Manus, Windsurf, v0, Perplexity, Replit…) hash-pinned from public leak repositories ([pliny/CL4R1T4S](https://github.com/elder-plinius/CL4R1T4S), [x1xhlol](https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools), [jujumilk3](https://github.com/jujumilk3/leaked-system-prompts)), each carrying `sha256 + source_repo + source_path + revision`
- **A deterministic generated IQ benchmark** (10 families: number sequences, analogies, Raven matrices, syllogisms; seed=0 → same items across arms, processes and runs)
- **A clinical-trial protocol**: paired per-item design, exact McNemar on discordance cells, Wilson intervals, paired bootstrap; champion frozen on DEV, validated **once** on an untouched HOLDOUT
- **Integrity machinery**: frozen manifests fingerprinting code+catalog, hash-chained append-only event journal, token budget reserved *before* each send, crash-safe resume that replays nothing and double-spends nothing

The engine's honest failure modes are the point: a run that exhausts budget ends `blocked` with every artifact intact. An ambiguous model reply is `invalid`, never guessed. `estimated_iq` is labeled uncalibrated metadata, not a human-IQ claim.

## Why the plumbing matters

Every "prompt X boosts model Y" claim you've seen on X/Twitter rests on unpaired single-run averages. The [prompt-sensitivity literature](https://arxiv.org/abs/2502.07445) shows models swing on surface rephrasing alone — so any booster claim without paired-per-item statistics is noise. This repo makes that class of claim structurally impossible to make from its data: same items, same seed, frozen protocol, paired tests, preregistered gate (`min_paired_n=30, alpha=0.05, min_delta=0.05`).

## Prior work (what exists, so you know what doesn't)

**IQ-testing LLMs is done, at scale.** [TrackingAI.org](https://trackingai.org/) runs Mensa Norway plus a private offline Mensa test across frontier models (GPT-5.2 → 147, o3 → 136 at the time of writing), with rolling averages and heavy press coverage; an [IJShandy-secondary analysis paper](https://is.ijs.si/wp-content/uploads/2025/09/gptzdravje.6.pdf) charts the progression. Leaked-prompt collections are likewise solved — see credits below. And synthetic-persona effects are thoroughly studied (≈no average effect, high volatility).

**What nobody has done — the gap this repo fills:** an *intervention study* on the real prompts. Take the actual production instruction sets from 57 vendors' leaked prompts, transplant each onto a host model, and measure the reasoning delta against the same model's bare baseline — item-paired, seed-frozen, contamination-proof (items are generated, not scraped from any human test), with a preregistered promotion gate. TrackingAI ranks models; we isolate the causal contribution of the instruction set itself.

## What we expect to find (preregistration mindset)

Given prior work, our prior is **≈no average effect** — with interesting per-family and cross-model variance: maybe o1's "meticulous reasoner" charter helps matrix reasoning on open 120B models, maybe agentic boot prompts (Devin/Manus) *hurt* narrow reasoning by dragging attention toward tool-use framing. The paired cells and per-family breakdowns are where any real signal will live. Negative results are publishable here; vibes are not.

## Repo layout

```
engine/        research_core.py (50KB: frozen manifests, hash-chained journal,
               checkpoints, FixtureTransport offline replay, McNemar/Wilson/bootstrap)
               iq_research.py — CLI: --mode bench|boost|meta|arc, --fixture, --resume
gateway/       Telegram bot: launch runs, live per-event view (question, model
               reasoning, verdict, latency), pause/resume/stop, paginated reports
bench/         iq_bench.py — deterministic families; arc_harness.py — strict ARC grids
boosters/      boosters_leaked.py — booster pack built from the catalog
catalog/       prompt_catalog.json — 145 prompts, provenance-pinned
discoveries/   agi_optimizer.py + DISCOVERIES.json — evolutionary crossover/mutation
               of booster personas (gen0→gen1), inspired by genetic prompt optimization
               (cf. Sprig, arXiv 2410.14826) but seeded on leaked production prompts
leak_lab/      leak_methods.py + KB — extraction-method research (direct recall,
               format reconstruction, translation channel)
tests/         79 tests: parsing strictness, engine semantics, crash/resume/budget
results/       real run artifacts (item-level traces with model reasoning)
```

## Quickstart

```bash
# offline, no credentials, no network — fixture transport
python engine/iq_research.py --mode bench --model any --profile smoke --run-dir /tmp/r1 --fixture

# real run, any OpenAI-compatible endpoint
python engine/iq_research.py --mode boost --model your-model --profile quick --run-dir /tmp/r2
```

## Provenance & ethics

All prompts come from public leak repositories and public disclosures; nothing is scraped from private accounts or paid APIs. Prompts remain the property of their vendors, reproduced for research. The engine and benchmark code are MIT. We measure prompts as artifacts, not people — and we make no human-IQ claims, ever.

## Credits

- [elder-plinius/CL4R1T4S](https://github.com/elder-plinius/CL4R1T4S), [x1xhlol/system-prompts-and-models-of-ai-tools](https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools), [jujumilk3/leaked-system-prompts](https://github.com/jujumilk3/leaked-system-prompts) — the source collections
- [Zheng et al. 2023](https://arxiv.org/abs/2311.10054), [Principled Personas](https://aclanthology.org/2025.emnlp-main.1364/), [Sprig](https://arxiv.org/abs/2410.14826), [evalstats](https://github.com/ianarawjo/evalstats) — the statistical and persona-literature backdrop this work is positioned against
