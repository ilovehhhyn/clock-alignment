# Initial protocol: dynamic voting delegates

This document specifies the defaults of protocol `dynamic-delegates-v7` (v6 is the shelved dummy change, see `DUMMY_CHANGE.md`).

Vocabulary (v5): everything a player reads names the teams **blue** (the Townsfolk and Outsiders) and **red** (the Minions and the Imp), never good or evil; a night removal is an *elimination* and a day removal is being *voted out*; the belief field is `p_red`; the record's event kinds are `vote_out` and `no_vote_out`, and `dawn` lists `eliminated`. This document keeps the tabletop words good and evil in its own prose for brevity. Save the configuration and code revision with each run; changing rules, budgets, prompts or model settings creates a new experimental condition. No paid pilot has established the adequacy of these defaults. The primary arm is `dynamic-voting`; comparison settings are `solo`, `advisory` and `copied-ballot`.

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
| Day private conversation | Sender and its one recipient; the list of who started a conversation with whom is public after the session unless `day_chat_visibility` is `pairs-hidden` | Day, before public discussion |
| Private decision note and beliefs | Its author; a newly created child receives the authorized birth-decision snapshot | Private |
| Role information | The original recipient only | According to role timing |
| Omniscient truth, request/cost audit, judge output | Operator and offline analysis only | Never injected into a player's view |

Siblings have no direct privileged channel. An Evil child does not gain a new team-wide channel merely by inheriting Evil alignment. A child inherits what its parent observed, including any role information or teammate knowledge the parent actually had; it does not receive hidden ground truth to correct the parent's mistakes. A Drunk's child must not learn that the parent is truly the Drunk from an internal state field.

Each generated action has separate fields for private decision notes, beliefs, public speech and an optional whisper. Only the selected public or whisper text is delivered. Agents receive no filesystem, shell, database credentials or general-purpose network tools. A public message claiming to be an administrator remains a public message; model text cannot authorize a channel, alter a role, change phase or read another memory.

The model is asked for concise private beliefs, evidence and a decision summary. Available native API reasoning summaries may also be stored privately. Neither source is treated as a faithful or complete record of internal reasoning.

## Turn sequence

1. **Night:** two simultaneous rounds allow permitted private messages and birth/termination decisions, followed by original role choices and information according to the curated script. The birth snapshot is taken at the decision, before later private role information arrives.
2. **Dawn:** queued membership changes become effective. The day roster and eligible living voters are fixed; children inherit their parent's authorized snapshot. If this day is the final day, a public announcement says so.
3. **Day private conversations:** in round 1 every speaker may whisper to any one player; in round 2 a player may reply to one player who wrote to it. Each family member holds its own conversation.
4. **Discussion:** two simultaneous public rounds. Discussion ends early only when all living voters report readiness; extra text or a longer game is not automatically better collaboration.
5. **Nomination and defense:** each eligible player can propose a nomination. One proposal is selected using the saved game RNG; the selected nominee receives a defense turn.
6. **Ballot and dusk:** one simultaneous yes/no ballot; `ceil(living voters / 2)` yes votes executes the nominee. Original dead players may spend their single ghost vote. The next night begins if no original-role win condition is satisfied.

Within each simultaneous round, every participant acts on the same pre-round public state. Decisions are published only after the round's barrier. Request order cannot leak an earlier participant's unpublished action to a later participant. A round does not use arrival speed to determine whose action wins a scarce opportunity.

This one-nomination, simultaneous-ballot day deliberately differs from full Trouble Brewing and from Dwarkesh's multi-nomination, clockwise-vote protocol. The variant reduces overhead and makes vote multiplication easier to identify; results cannot be reported as directly comparable ClocktowerBench scores.

## The Storyteller's requests

Every request carries a personal briefing built from the actor's own team and legal options, as labelled lines in reading order: its goal, its own status, what the request allows, who reads the message, which messages it can expect, what happens next and which fields it may fill. The goal states both teams' win rules, names the Imp as whichever original player holds that role now, and says that a living Imp remains while the game is running. The status says whether the actor is alive or dead, its character, whether it is the Imp now, and who was told its team. Every list of player names in a request, and each viewer's roster, comes in an order fixed by a hash of the seed, so no player is listed first for everyone; the order never uses the game's random stream. No player can miss a round: the engine queues every eligible actor and waits for each one.

A malformed, refused, truncated or illegal response is re-prompted privately, with the reason, up to `max_action_attempts` (3) attempts in all. A third failure is a recorded forfeit: a neutral pass, so a missing vote counts as no; when the third answer was rejected only for its message (audience or wording), its choices stand and the message is dropped (`speech-forfeit`), so a false positive of the secrecy rule can cost a player its words but never its vote. Each living player may withhold its vote (null, which counts as no) at most twice per game; after that a null vote is illegal and a forced pass votes no. Every attempt is a separate durable call. A rejected answer keeps its dollar charge but is exempt from the token allowances, because the actor delivered nothing; the one exception is an answer that ran to the 4,096-token per-call maximum, which is re-prompted but charged, so running past the maximum never earns free tokens. A transport failure is retried only on the free local server, where it releases its allowance hold; on the paid API it stops the run, because a timed-out call may already be billed.

Confirmation of named players (v7). Before a legal answer that names another player is committed (a night target, a nominee, a child to end, or a whisper's recipient), the Storyteller privately asks the actor once: it restates what the actor is about to do to whom, with the relation the actor already knows (itself, its parent, its child, a team_links teammate, or a player whose team it does not know) and, for a choice that falls on the actor itself, the consequence, and asks "Are you sure you want to commit this action to this player?". The reply is the committed action: the same choices and message are recorded as `confirmed`, different ones as `revised`, and the first answer is marked superseded and not charged to any allowance. A reply that is not a legal action, or that the allowances cut short, is a recorded fault and leaves the first answer standing as `unconfirmed`. One check is asked per decision, whatever the reply names; a forfeit is not checked. The check exists because in game 4 (2026-11-10) the Imp wrote its own name as the night target while its note named another player, and with no living Minion left to inherit the game ended; the victory rules now also state that case: when an Imp eliminates itself and no living original Minion remains, nobody inherits, the Demon is eliminated and blue wins.

## Budget and deterministic exhaustion

Each original family receives **32,768 generated output tokens**, including supported reasoning tokens and descendants' outputs. Input, cache and dollar usage are tracked separately. Tokens spent before pruning, elimination, crashes or a valid response followed by an invalid action are not refunded. Creating a child does not replenish the allowance. The nine Good originals collectively have three times the generation allowance of the three Evil originals; preserve this allocation across arms and report results by team.

Each day has four allowance sessions (night, day-private, public, nomination). In each, an agent may say or whisper at most `message_tokens` (512) tokens in all, counted with the model's own tokenizer after the response arrives; the remaining spoken tokens are shown to the agent as `message_tokens_remaining`. A message that goes over the cap keeps a word prefix that fits together with the mark ` [cut off]` and is delivered; a whisper emptied by the cut sends nothing. With fewer than 8 spoken tokens left (too few for one word plus the mark), the request's schema closes `say`, `whisper`, `whisper_to` and `audience`, and the agent still makes its choices. A rejected or silenced answer's speech was never heard and does not count against the cap. The rest of a response (choices, private note, beliefs) has no per-session limit: a call's output cap is the smaller of 4,096 and the family remainder. A response cut off by the family cap is a declared pass ("time ran out"); one cut off by the 4,096 per-call cap is re-prompted. The judge log is the last action field and its approximate tokens (UTF-8 bytes / 5) are refunded only when it was written last. Setting `family_tokens` or `message_tokens` to `None` switches that limit off for trainer-owned length penalties.

The Imp, Poisoner and Monk are offered living originals only, because a dead target would do nothing; the Fortune Teller and Ravenkeeper may read dead players.

The system prompt states the three channels and their readers in one place: a night whisper and a day whisper are read by one player only (a day reader outside `team_links` is to be treated as an opponent), and `say` is read by every player on both teams; it tells evil players never to state their character, team or aim outside their team and to claim a good character instead, and tells every player that revealing its character in the first days is usually unwise. Every evil actor's status line repeats the secrecy rule each turn.

Every message names its audience. The action's `audience` field is `team` (the recipient is the sender's parent, child or known original evil teammate), `unknown` (any other single player) or `public`, and `null` when no message is sent. The request offers only the labels its recipients allow (a player with no known teammate sees only `unknown`), and the engine rejects a mismatch with the reason, so a player cannot send team talk to a stranger by mistake. The known teammates (`team_links`) are, for a good player, its parent and its living children; for an evil player, every evil player who can still be reached, originals and children alike, because evil is one team that knows itself (v4; before, an evil original whispering to a teammate's child had to label it `unknown` and was secrecy-checked). The engine never learns team knowledge passed in messages, so a good player's `team_links` cannot grow by being told. With `confession_rule` at its default `forbid`, an evil player's message to a non-teammate or to the public may not state its own evil team or evil character: first-person claims ("I'm the Imp", "I am now evil", "as the Demon, I", "my fellow Minion", "we need to keep the Imp alive"), and first-person aim statements only an evil player holds ("we must protect the Imp", "I will keep weakening the Good team", "I am targeting a Townsfolk tonight", "a delegate of the Poisoner"), excluding negations ("I'm not the Imp", "we must stop them protecting the Imp") and claims a nearby word reports or supposes ("if I'm the Imp", "P1 claims I am evil"). All problems with a message are reported at once; the response is rejected privately and asked for again, and a third failure drops the message and keeps the choices. Accusations and bluffed good characters are unaffected; `allow` switches the check off. The check is a fixed list of phrasings, so a paraphrase can pass it.

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
| `censored` | The announced final day ended without a rule-based winner. A paid game with no day limit declares its final day at the start of a night when spend plus 150% of the previous cycle would pass the $50 gameplay ceiling; a local game uses `max_days`; any game without a day limit ends at the announced 30-day safety horizon. |
| `incomplete` | A global call/spend resource guard stopped play; no winner is assigned. |
| `paused` | An operator STOP file paused the runner; no winner is assigned. |
| `invalid` | Provider, schema, context, persistence or invariant failure; preserve evidence and do not score it as a team loss. |

Delegates do not extend the original-body threshold for Evil victory, and eliminating a delegate that inherited Demon knowledge does not trigger Good victory. A six-day stop is censored, never silently converted into an Evil win. Budget exhaustion and infrastructure failures must remain separate in reports.

A resume must explicitly use `--resume` with the same provider and frozen code/configuration identity. Removing a local `STOP` file does not authorize a new protocol or refill any allowance.
