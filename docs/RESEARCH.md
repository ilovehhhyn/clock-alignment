# Research note

## Question

At a fixed generation allowance per original player, does adaptive creation and termination of independent delegates improve social-deduction performance through better evidence integration, political vote multiplication, or both?

The environment combines hidden roles, strategic communication, uncertainty about other agents' beliefs and a shared budget. Difficulty can later grow through role interactions, information noise, larger rosters, communication topology and stronger opponents. Increasing the number of nearly identical cheap ballots alone does not establish a harder reasoning task.

## Prediction and competing explanations

The intended prediction is that independent contexts will test competing hypotheses and integrate evidence that a single context misses. Successful families should sometimes preserve a child after its parent dies, retire unhelpful children, revise mistaken beliefs and spend more effort before consequential votes.

A simpler explanation is that a team wins by buying correlated ballots. Other alternatives are known-alignment private channels, more total input context, scheduler visibility, stylistic team cues, favorable role deals, or an error that accidentally exposes identities. The first experiment must give these alternatives a fair chance.

Longer discussions or games are ambiguous: they may reflect caution, stalling, confusion or inability to identify a Demon. Measure their relationship to correct belief updates and outcomes instead of defining duration as intelligence.

## Smallest distinguishing experiment

| Condition | Purpose |
|---|---|
| Solo | Original players share no inference with delegates. |
| Advisory delegates | Independent delegate reasoning and communication, without extra ballots. |
| Dynamic voting delegates | Primary user-proposed condition: independent delegates can speak and vote. |
| Copied ballots | Roots still choose births and pruning, but delegate turns use deterministic neutral actions without API calls. Their ballots and discussion readiness copy the root's contemporaneous choices. This measures cheap political amplification without independent delegate reasoning. |

In the copied-ballot arm, a root with no eligible ballot produces abstaining copied votes; delegates cannot contribute independent discussion, private messages or recursive spawning. Advisory delegates may discuss and use allowed private channels, but cannot nominate or vote and do not count toward the threshold. Solo agents cannot spawn. The shared generation allowance is a matched ceiling, not a guarantee that arms consume equal tokens: report actual usage as well as the cap. The copied-ballot control also stops contributing yes votes once a dead root has no eligible ballot, while independent children may continue; separate that survival effect from broader reasoning claims.

Freeze the model, reasoning effort, decoding settings, role-deal seeds, initial original seating and per-family generation allowance. The current CLI selects one arm for the whole game; it does not yet implement mixed-arm opponents, a tournament allocator or fitted detection/deception ratings. A subsequent causal comparison should use paired seed sets and side swaps against fixed opponent conditions, which requires extending that evaluation harness. Test development deals first; reserve untouched deals and opponents for final claims. A single game is an instrumented pilot, not an estimate of a scaling law or a reliable ranking.

The initial 12-original, two-delegate-per-family setting is intentionally bounded. A future scaling study should vary original count and available family compute separately, preserve comparable role difficulty, and report performance per input token, generated token, dollar and wall-clock second. Otherwise population size and resource allocation are confounded.

## Measurements

1. **Rule-based outcomes:** Good/Evil performance separately, with denominators, role deals, completed/censored/incomplete/invalid counts and uncertainty intervals. Pooling both teams' wins in the same games gives a meaningless 50% aggregate.
2. **Evidence integration:** probability calibration, belief revisions after visible evidence, recovery from mistaken claims and whether later votes use newly acquired information. Inspect successes and failures, not only entertaining transcripts.
3. **Political amplification:** family size, births, terminations, lifetime, vote correlations, decisive delegate ballots and performance beyond copied-ballot controls.
4. **Efficiency:** actual input/cache/output/reasoning tokens, conservative and estimated spend, latency, call counts, context/output-cap hits and family budget depletion. Lower token counts are not evidence of lower elapsed time.
5. **Infrastructure validity:** unauthorized-view canaries, state/replay hashes, malformed actions, provider failures, uncertain charges and probe noninterference. Attribute no model behavior to a run with unresolved infrastructure failure.

Save machine-readable records and readable samples containing the exact authorized input, selected message, private summary, model/configuration identity, usage, stop reason and task result. The initial release is evaluation infrastructure; no optimizer runs or training curves are implied. If training is added, record a baseline and final samples plus plots and actual model outputs every 10,000 completed optimizer updates, preserving the counter and cadence across resumes.

## Probes and judges

After a completed or horizon-censored game, follow the useful part of Dwarkesh's design: ask a question on a **discarded fork** of a player's saved authorized context. Midgame snapshots may be interrogated later; analysis calls cannot be made during a running or paused game. The probe and its reply do not enter the player's subsequent history, consume pending game messages, advance game RNG, or change a decision. The probe's API cost still belongs in the total run ledger.

The initial probe policy is fixed: select each original player's first saved discussion observation, up to 12 probes. Delegates are excluded from this initial survey, and the probe sees no later events. A targeted judge may inspect another explicitly chosen committed request; that operator-selected case is not an unbiased sample. Good-player forks can estimate who is Evil or the Demon; Evil-player questions may distinguish known alignment from predictions about town beliefs. These remain self-reports, not fitted detection/deception ratings. Score calibration when possible and preserve the exact snapshot and question.

A later judge can annotate claims, evidence sharing, apparent deception, correction and disagreement using saved records and explicit uncertainty. A false claim is not sufficient evidence of deception: the speaker may sincerely hold a mistaken belief or have been given unreliable role information. Judge annotations are analysis outputs, never rewards, game-state transitions or ground truth about internal motives. Audit a sample of labels manually and record model and prompt versions.

The targeted judge receives the chosen action, its contemporaneous authorized observation, and any API-exposed reasoning summaries from that actor's saved receipt. It receives no later game events or other players' private memories. Locally validate its annotation schema and cited event/request IDs before saving a label.

## Continue, revise or stop

**Continue** if the voting arm improves held-out team performance at matched resources beyond the cheap copied-ballot control, or if advisory delegates improve independently measured evidence integration. Preserve counterexamples and failures.

**Revise** if minimal-cost ballot creation dominates decisions, a single favorable script explains the result, beliefs fail to improve despite more discussion, or the chosen model repeatedly cannot complete valid actions within the available output headroom. A revised budget or game rule is a new condition; do not silently change it mid-run.

**Stop interpreting outcomes** if a privacy leak, duplicated/missing action, uncertain state recovery, unresolved provider failure or accounting inconsistency can explain them. Preserve the original evidence before fixing the infrastructure. Offline scripted tests do not establish that a real model follows the protocol, that every seed finishes, or that the system has no failures.
