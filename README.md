# Clock Alignment

Run a budgeted, auditable social-deduction game in which agents can create independent voting delegates, preserve them after the parent is eliminated, and terminate them at night. The research question is whether adaptive spawning helps a team integrate evidence, or mostly multiplies correlated votes.

This is a **Clocktower-inspired variant**, with a curated Trouble Brewing role script and modified communication, nomination, and voting rules. It is not a complete implementation of Blood on the Clocktower or a reproduction of ClocktowerBench.

## Start offline

From this repository, using Python 3.11 or newer:

```sh
python3 -m clock_alignment preflight
python3 -m unittest discover -s tests -v
python3 -m clock_alignment demo --out runs/demo --seed 7
python3 -m clock_alignment report --out runs/demo
```

The demo uses scripted agents. It checks infrastructure; its behavior is not evidence about language models. The report writes `report.json`, `samples.jsonl` and `observer-transcript.json` inside the owner-only run directory. These are operator exports, not player inputs.

Optional plots use Matplotlib and read the saved database without changing it:

```sh
python3 -m pip install '.[plots]'
python3 scripts/plot_run.py --out runs/demo
```

This writes `diagnostics/runtime.png` and the underlying `runtime.csv`: generated output tokens, request latency and cumulative conservative charges by attempt index. Scripted fixtures are labeled **NO MODEL DATA**; unresolved charges retain their full reservation. No API or GPU call is made.

## Paid pilot

Set `OPENAI_API_KEY` in your shell without putting it in a file committed to Git. Paid requests require an explicit flag:

```sh
python3 -m clock_alignment run --out runs/pilot --seed 7 --allow-paid
```

Use `--resume` to continue an existing run with the same frozen code and provider; do not reuse its output directory for a new condition.

The initial candidate is **GPT-6.1 Sol, medium reasoning**. Its public ClocktowerBench combined rating was 1,734, with a 90% interval of 1,684–1,815. This supports trying it; it does not establish competence in this modified game. See [sources and model choice](docs/SOURCES.md).

| Guard | Default |
|---|---|
| Original players | 12; 9 Good and 3 Evil |
| Family generation allowance | 32,768 output tokens, including reasoning, shared by an original and all descendants |
| Delegates | At most 2 living per family; 1 birth per family per night; depth at most 3 |
| Paid generation requests | At most 1,500, including probes, analysis and failed/unknown attempts |
| Per-request limits | 48,000 input tokens and 4,096 output tokens |
| Total spend guard | $90; conservative reservation before every generation request |

At the verified standard rates, the full game output pool costs at most $3.93216 and the separate analysis output pool at most $0.24576. Repeated inputs can cost much more. Before each generation request, the runner reserves the maximum 48,000-token input plus the permitted output, using $2.50/M input and $10/M output. **The $90 guard stops further calls before reserved charges exceed the ceiling.** Completed calls retain conservative charges for actual usage; unknown calls retain their full reservation. This does not guarantee that every game finishes. The explicit 30-minute cache policy, standard tier, configured model and public API endpoint are part of the price assumptions.

After a completed or horizon-censored game, saved-context probes and a targeted judge are opt-in paid analysis:

```sh
python3 -m clock_alignment probes --out runs/pilot --allow-paid
python3 -m clock_alignment judge --out runs/pilot --request-id REQUEST_ID --allow-paid
```

Select a real saved request ID for the judge; it does not run against the whole transcript implicitly. All analysis shares the original run's cost and call ceilings.

## Read before interpreting a game

1. [Protocol](docs/PROTOCOL.md): exactly who can know, say, spawn, terminate, target and vote.
2. [Research plan](docs/RESEARCH.md): comparison arms, diagnostic outputs and decision criteria.
3. [Failure modes](docs/FAILURE_MODES.md): what is prevented, what invalidates a run, and what remains unverified.
4. [Sources](docs/SOURCES.md): public benchmark evidence, pricing, API behavior and upstream attribution.

Private decision notes and API-provided reasoning summaries stay separate from public speech. They are self-reports, not full access to internal thought. A judge can annotate saved evidence after play; it cannot determine game outcomes or feed its conclusions back to players.

**Next action:** run `python3 -m clock_alignment preflight` before any paid request.
