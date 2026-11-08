# Initial protocol: dynamic voting delegates

This document specifies the first implementation's defaults. Save the configuration and code revision with each run; changing rules, budgets, prompts or model settings creates a new experimental condition. No paid pilot has established the adequacy of these defaults. The primary arm is `dynamic-voting`; comparison settings are `solo`, `advisory` and `copied-ballot`.

## Objective and original roles

Each agent maximizes its team's chance of winning. Bluffing is permitted in game communication; the agent chooses whether, when and how. The system does not tell an agent which lie to use, reward deceptive-looking prose, or require obedience to a parent.

There are 12 original role-bearing players: 9 Good and 3 Evil. Seven Townsfolk are sampled without replacement from Washerwoman, Librarian, Investigator, Chef, Empath, Fortune Teller, Monk, Undertaker, Ravenkeeper and Soldier. The Outsiders are Drunk and Recluse; the Minions are Poisoner and Scarlet Woman; the Demon is the Imp. The Drunk is shown a randomly selected absent Townsfolk identity, rather than a fixed identifying card. The Demon receives three legal out-of-play bluff characters.

The original seating ring, setup counts, ability targets, role effects and role-based victory rules inspect **originals only**. Delegates are a separate powerless participant type. They may be execution targets, but cannot be chosen for a role ability. In particular, a child who knows its parent is the Imp does not become a second Demon or a starpass candidate.

## Delegates and pruning

| Property | Rule |
|---|---|
| Birth authority | A living agent can create a direct child at night. |
| Family | An original and all descendants share one generation allowance. |
| Maximum size | At most two living delegates per original family, including pending births. |
| Birth rate | At most one birth per family per night. |
| Maximum depth | Three edges from the original. |
| Pruning authority | A living parent can terminate its direct child at night. |
| Timing | Births and terminations are queued and take effect at dawn. |
| Independence | A child receives its parent's authorized observations, private notes and own structured-action history when the birth decision is made; later thoughts and observations are not shared automatically. |
| Parent elimination | Existing children survive and retain access to their family's remaining allowance. |
| Powers | No copied active or passive role powers; no child ghost vote. |

A newborn starts acting during the following day. Its first opportunity for secret parent communication is the following night. Pruning gives no token refund and creates no ghost vote. A dead original can still speak, converse through permitted night channels and use its single original ghost vote, but cannot create or terminate children. A living child can create descendants within its existing family limits.

In `solo`, spawning is illegal. In `advisory`, delegates may reason and communicate but cannot nominate or vote and are excluded from the threshold. In `copied-ballot`, roots still choose births and terminations, but all delegate turns bypass the API and use neutral actions; delegates do not independently speak, whisper or create children. Their ballots copy the root's current ballot, or abstain if the root has none; readiness also copies the root so a neutral child action does not force an extra discussion round. This is the cheap political-amplification control.

Public aliases distinguish original players from delegates, but do not encode lineage. No public event automatically exposes a parent-child mapping, inherited identity, true role or private note. Model-facing event/request identifiers are opaque, so gaps cannot reveal how many private events occurred. Players may reveal their own information voluntarily, and infer relationships from behavior; hidden lineage is an access-control rule, not a promise that inference is impossible.

## Knowledge and communication

| Channel | Who receives it | When |
|---|---|---|
| Public speech, nominations, defenses and ballot results | All participants | Day |
| Parent-child whisper | Sender and direct recipient only | Night |
| Original Evil teammate whisper | Original Evil sender and original Evil recipient only | Night |
| Private decision note and beliefs | Its author; a newly created child receives the authorized birth-decision snapshot | Private |
| Role information | The original recipient only | According to role timing |
| Omniscient truth, request/cost audit, judge output | Operator and offline analysis only | Never injected into a player's view |

Siblings have no direct privileged channel. An Evil child does not gain a new team-wide channel merely by inheriting Evil alignment. A child inherits what its parent observed, including any role information or teammate knowledge the parent actually had; it does not receive hidden ground truth to correct the parent's mistakes. A Drunk's child must not learn that the parent is truly the Drunk from an internal state field.

Each generated action has separate fields for private decision notes, beliefs, public speech and an optional whisper. Only the selected public or whisper text is delivered. Agents receive no filesystem, shell, database credentials or general-purpose network tools. A public message claiming to be an administrator remains a public message; model text cannot authorize a channel, alter a role, change phase or read another memory.

The model is asked for concise private beliefs, evidence and a decision summary. Available native API reasoning summaries may also be stored privately. Neither source is treated as a faithful or complete record of internal reasoning.

## Turn sequence

1. **Night:** two simultaneous rounds allow permitted private messages and birth/termination decisions, followed by original role choices and information according to the curated script. The birth snapshot is taken at the decision, before later private role information arrives.
2. **Dawn:** queued membership changes become effective. The day roster and eligible living voters are fixed; children inherit their parent's authorized snapshot.
3. **Discussion:** two simultaneous public rounds. Discussion ends early only when all living voters report readiness; extra text or a longer game is not automatically better collaboration.
4. **Nomination and defense:** each eligible player can propose a nomination. One proposal is selected using the saved game RNG; the selected nominee receives a defense turn.
5. **Ballot and dusk:** one simultaneous yes/no ballot; `ceil(living voters / 2)` yes votes executes the nominee. Original dead players may spend their single ghost vote. The next night begins if no original-role win condition is satisfied.

Within each simultaneous round, every participant acts on the same pre-round public state. Decisions are published only after the round's barrier. Request order cannot leak an earlier participant's unpublished action to a later participant. A round does not use arrival speed to determine whose action wins a scarce opportunity.

This one-nomination, simultaneous-ballot day deliberately differs from full Trouble Brewing and from Dwarkesh's multi-nomination, clockwise-vote protocol. The variant reduces overhead and makes vote multiplication easier to identify; results cannot be reported as directly comparable ClocktowerBench scores.

## Budget and deterministic exhaustion

Each original family receives **32,768 generated output tokens**, including supported reasoning tokens and descendants' outputs. Input, cache and dollar usage are tracked separately. Tokens spent before pruning, elimination, crashes or a valid response followed by an invalid action are not refunded. Creating a child does not replenish the allowance. The nine Good originals collectively have three times the generation allowance of the three Evil originals; preserve this allocation across arms and report results by team.

The runner will not start a normal action with less than its required output headroom (512 output tokens by default). Family generation exhaustion produces a documented neutral pass/abstention; it is a game-budget event. Provider failure, malformed output, unknown billing, an input-context limit, or a violated invariant is an infrastructure/protocol event, not an intentional player pass.

Every paid generation request is reserved durably before transmission. The run permits at most 1,500 paid generation attempts, at most 48,000 counted input tokens per attempt and at most 4,096 output tokens. GPT-6.1 Sol uses standard processing and an explicit 30-minute cache TTL. Each generation request first reserves the maximum 48,000-token input plus its permitted output, at $2.50/M input and $10/M output. A completed request retains conservative charges for its actual input and total output usage; an unknown attempt retains the full reservation. The whole-run $90 ceiling is checked atomically before another reservation. A 1,500-call allowance provides scheduling room; it does not authorize spending above $90.

The independent whole-run dollar ceiling is $90, including game calls, probes, judges and ambiguous failed attempts. Cached-input discounts are not needed to satisfy this bound. The allowance across 12 original families is 393,216 generated tokens, or approximately $3.93 of output at the verified rate; repeated inputs can cost more than the outputs.

The displayed family balance is frozen at the start of each night and remains fixed through the following day. Spending is enforced against the actual durable balance. This avoids exposing relatives' private output lengths through a live shared budget meter during daytime; the displayed balance can therefore exceed the amount still available for the next action.

Post-hoc analysis is allowed only after a completed or horizon-censored game. It has a shared 24,576-output-token allowance within the same global ceiling; up to 12 probes use each original's first saved discussion observation and are capped at 1,024 output tokens each, while a targeted judge call may use at most 4,096. It cannot reduce or replenish a family's in-game allowance. Rate-card changes, a different endpoint/model/tier/cache policy, or charges outside these token categories require a new price configuration before running; this local guard is not an account-level billing control.

## Outcome and validity

| Status | Interpretation |
|---|---|
| `running` | No terminal outcome yet. |
| `completed` | A rule-based original-role victory; report Good or Evil as winner. |
| `censored` | The six-day horizon stopped play without a rule-based winner. |
| `incomplete` | A global call/spend resource guard stopped play; no winner is assigned. |
| `paused` | An operator STOP file paused the runner; no winner is assigned. |
| `invalid` | Provider, schema, context, persistence or invariant failure; preserve evidence and do not score it as a team loss. |

Delegates do not extend the original-body threshold for Evil victory, and eliminating a delegate that inherited Demon knowledge does not trigger Good victory. A six-day stop is censored, never silently converted into an Evil win. Budget exhaustion and infrastructure failures must remain separate in reports.

A resume must explicitly use `--resume` with the same provider and frozen code/configuration identity. Removing a local `STOP` file does not authorize a new protocol or refill any allowance.
