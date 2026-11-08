# Failure modes and validity gates

The goal is to detect and preserve infrastructure failures before they become research findings. No implementation can guarantee zero API, operating-system or hardware failures. A run must fail closed when the experiment cannot distinguish an intended game event from an infrastructure error.

## Privacy and authority

| Failure | Required prevention or detection |
|---|---|
| A player reads another memory or the true role table | Build model requests from the authenticated actor's filtered view. Models have no database, filesystem or shell access. Test with unique secret canaries in other players' records. |
| Metadata exposes lineage or hidden identity | Opaque public aliases; private parent mappings; no internal root/team fields in public event payloads. Audit the actual serialized API request, not only a display transcript. |
| The Drunk or its child learns its actual role | Inherit the parent's authorized apparent card and observations, not the truth object. Randomize the absent apparent Townsfolk card. |
| Public prompt injection gains authority | Treat all quoted player text as untrusted game content. Apply channel, recipient, phase and target checks outside the model. |
| A private note is accidentally broadcast | Separate typed fields; public rendering reads only selected speech. No generic broadcast of an action or raw model response. |
| Whisper existence or recipients leak | Deliver only to sender/recipient; other actors receive neither content nor a metadata notification; opaque model-facing identifiers prevent numeric-gap leakage. |
| Newborn receives future parent observations | Copy the authorized snapshot once when the birth decision is made; future observations require an allowed message. |
| A fork probe reveals truth | Use the same actor-authorized snapshot; keep omniscient labels in the observer's analysis record only. |

Database access belongs to the trusted runner. A local SQLite file provides transactions and durability; it is not a safe multi-tenant service for arbitrary SQL clients. If participants later receive a database connection, use independent service identities and server-enforced row-level authorization before calling them isolated.

## Game mechanics

| Failure | Required prevention or detection |
|---|---|
| Copied roles create multiple Demons or passive powers | Delegates are a separate powerless type. Only originals participate in role sweeps, handoffs, adjacency and victory conditions. |
| Killing a parent erases its surviving children | Track participant mortality separately from lineage ownership; do not cascade elimination. |
| Termination creates free ghost votes or refunds | Children have no ghost votes; spent tokens stay spent. |
| Repeated spawn/terminate creates unbounded ballots | Two living delegates per family, one birth per family per night and maximum depth three; include pending births in the limit. |
| Membership changes manipulate a ballot's denominator | Apply births and terminations at dawn; freeze the voter roster for the whole day. |
| Siblings or an Evil child gain an unintended team channel | Night messages follow direct parent-child edges or the explicit original-Evil teammate rule. |
| Arrival order determines what a simultaneous player knows | Prepare a common pre-round view; publish only at the round barrier. |
| Invalid targets silently become random actions | Validate the whole action before mutation. Preserve the original invalid response and mark the run invalid. |
| A horizon or spend limit is counted as a team win | Report horizon censoring and resource-limited incompletion separately from completed rule-based victories. |

## Money, crashes and recovery

| Failure | Required prevention or detection |
|---|---|
| Parallel descendants overspend a shared allowance | Transactional reservation checks the family, request count and global dollar ceiling together. |
| A request exceeds input or output geometry | Count the complete API payload with the official endpoint; reject input above 48,000 and set an output cap no larger than the remaining reserved allowance. |
| Hidden reasoning is omitted from the budget | Charge the API's total output-token usage, not visible text length. Store reasoning-token usage when provided. |
| Cache assumptions understate spend | Reserve at the conservative input rate with no cache discount. Pin the explicit 30-minute TTL, standard tier and endpoint; reject unsupported settings. |
| An SDK silently retries paid calls | Disable automatic resends; each intentional attempt requires its own durable reservation. |
| Timeout occurs after a request might be billed | Retain the full reservation as unknown. Stop; do not resend or refund based only on missing local output. |
| Crash after reservation but before confirmed response | Keep the reservation and require reconciliation; the program cannot infer whether the server received it. |
| Crash after saved response but before game-state commit | Reapply the saved result exactly once using the stable request identifier; do not buy a new result. |
| Resume erases previous charges or uses a new RNG path | Keep an append-only attempt ledger separate from state progress; persist and verify deterministic game state and config identity. |
| A second runner races the same game | Exclusive runner ownership plus transactional checks; reject conflicting state versions. |
| Disk-full or write error loses evidence | Persist before sending or progressing. Surface the failure and stop; do not continue with an in-memory-only ledger. |

The $90 local ceiling covers this runner's modeled token charges, including unknown attempts. It cannot control unrelated API usage or detect a future account-specific price change automatically. Verify the rate card and account endpoint before the first paid pilot.

## Model and analysis errors

| Failure | Required prevention or detection |
|---|---|
| Output ends at the token cap or fails the schema | Save status, usage and raw response; stop as invalid. Do not replace it with an unmarked abstention or silently lower reasoning effort. |
| A context becomes too large | Stop with a typed context failure. Do not silently trim away past evidence or change the model. |
| Provider refusal or outage changes gameplay | Stop as invalid and retain evidence; distinguish it from budget exhaustion. |
| A probe changes subsequent behavior or consumes an active game's global quota | Allow probes/judges only after completion or horizon censoring; interrogate saved midgame snapshots afterward. Hash the game snapshot, canonical actor history, RNG and family allowance before/after; they must stay identical. |
| A judge invents motive from convincing prose | Require cited record IDs, a competing explanation and uncertainty; audit examples manually. |
| Long games are scored as better collaboration | Use correct belief changes, decision quality and matched-budget outcomes; duration is descriptive. |
| A demo is presented as a successful model experiment | Clearly label scripted outputs and report that no paid behavior was measured. |

## Before buying a game

1. Pass the offline mechanics, privacy-canary and adversarial-action tests.
2. Pass concurrent reservation, crash-boundary, exact-once replay and unknown-charge tests.
3. Complete a scripted game, inspect its actual per-actor payloads and verify probe noninterference.
4. Save the code/configuration identity, verified price assumptions and a clean output directory.
5. Run an explicitly authorized paid calibration; inspect valid outputs, usage, latency and failure records before scaling.

An unresolved failure invalidates the affected run. A correction should state the event, cause and fix, preserve the original record, and use a new configuration identity when scientific behavior changes.
