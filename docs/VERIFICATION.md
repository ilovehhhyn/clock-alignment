# Verification record — November 8, 2026

The implementation was developed using the research taste workflow, with an independent mechanics and accounting critic. This record describes offline verification, not measured language-model performance.

| Check | Observed result |
|---|---|
| Standard-library test suite, Python 3.14.6 | 81 tests pass; approximately 50 seconds |
| Four-arm offline preflight | Solo, advisory, copied-ballot and dynamic-voting fixtures reach a natural win or the configured horizon without infrastructure errors |
| Full six-day scripted fixture, seed 7 | 806 decisions, 24 delegate births, 17 terminations, 280 public discussion messages and 238 permitted private messages; horizon-censored, no winner assigned |
| Paid API requests | Zero; fixture token/cost entries are synthetic and must not be used as a real cost estimate |
| Optional Matplotlib diagnostics | PNG rendered and visually inspected; underlying CSV retained; fixture clearly labeled |

Regression tests cover secret-view filtering, night-only channels, powerless delegates, parent-death survival, original-only victory, ghost votes, simultaneous rounds, truthful/false role information, pruning, readiness, and canonical checkpoint restoration. Transport/accounting tests cover concurrent reservations, unknown billing, invalid/capped responses, exact-count context guards and response reuse after a crash without another network call. Analysis tests check that probes and judgments do not modify gameplay state, RNG or lineage allowances.

Code review caught and fixed a Drunk-card identity leak, missing common rules, loss of an actor's own past actions, global sequence metadata leaks, key-order-dependent replay, validation bypass on saved receipts, malformed usage losing its receipt, nonchronological probe selection, and stopped-run status reporting. These are regression cases, not claims that every possible failure is prevented.

The live API request contract, account/model access, model competence, real latency, real cost and completion frequency remain unmeasured. No GPU allocation, training run or optimizer update occurred. The first paid run should be treated as calibration, within the frozen $90 local spending guard.

Run the checks from a clean checkout:

```sh
python3 -m unittest discover -s tests -q
python3 -m clock_alignment preflight
```

Generated run artifacts stay outside Git in the ignored `runs/` directory. Each run stores its configuration, source fingerprint, durable attempts and exact authorized views. The CI workflow repeats offline tests and preflight on Python 3.11 and 3.13; its result is separate from this local verification record.
