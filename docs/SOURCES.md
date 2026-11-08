# Sources and model choice

Verified November 8, 2026. Pricing and availability can change; these links support the initial configuration, not an indefinite billing guarantee.

## Why GPT-6.1 Sol

Dwarkesh's published ClocktowerBench data reports GPT-6.1 Sol with a combined rating of **1,734**, 90% interval **1,684–1,815**, versus GPT-5.6 Sol at **1,591**, interval **1,543–1,647**. GPT-6.1 Sol had 37 Good-side and 37 Evil-side games in that published table. This is directly relevant evidence of social-deduction competence, but the benchmark used a different protocol and does not validate this environment or a particular reasoning-effort setting. [Published ratings](https://github.com/dwarkeshsp/clocktower/blob/c5d9696b42212f4bd4a99c0b5de831fa0643110b/docs/plans/f2-ratings.json).

The initial setting is **medium reasoning**. This is a pilot design choice balancing output headroom and cost, not a claim that medium reproduces the published score. OpenAI lists medium as the model's default and supports reasoning tokens. [Official model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

## Prices and accounting

| GPT-6.1 Sol category | Standard price per million tokens |
|---|---|
| Input | $2.00 |
| Cached input | $0.10 |
| Short cache write | $2.50 |
| Long cache write | $5.00 |
| Output | $10.00 |

The run uses `prompt_cache_options: {"ttl": "30m"}`, reserves all input at the conservative 30-minute cache-write rate, and does not rely on cache hits. A 12-hour TTL has a higher write price. The older `prompt_cache_retention: "in_memory"` setting is not the configured API contract. The configured 30-minute cache TTL matters: using long retention, faster processing, a regional endpoint or another model can change the bound. [Official model price](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [official pricing and endpoint uplifts](https://developers.openai.com/api/docs/pricing), [official prompt-caching API guide](https://developers.openai.com/api/docs/guides/prompt-caching).

The official input-count endpoint accepts the complete Responses payload and counts structural tokens as well as visible text. Total reported output usage includes non-visible tokens; `max_output_tokens` limits all generated output, including reasoning. These are why the budget uses API usage and counted complete requests rather than characters or visible words. [Official token-counting guide](https://developers.openai.com/api/docs/guides/token-counting).

The Responses API can return an opted-in reasoning summary. That is not an assurance that all internal reasoning is exposed, faithful or complete. [Official reasoning-summary documentation](https://developers.openai.com/api/docs/guides/reasoning).

## Methodology borrowed from ClocktowerBench

The benchmark's useful design choices include scripted rule resolution, separate per-seat views, explicit public/private channels, frozen protocol versions, side-specific analysis and belief questions asked on discarded forks. A fork includes the player's authorized pending context, records the answer separately and does not append it to the real game conversation. The economical adaptation here uses sparse saved snapshots rather than probing every seat at every opportunity. [Project and methodology](https://www.dwarkesh.com/p/clocktower), [probe implementation](https://github.com/dwarkeshsp/clocktower/blob/c5d9696b42212f4bd4a99c0b5de831fa0643110b/clocktower/probe.py), [protocol](https://github.com/dwarkeshsp/clocktower/blob/main/docs/PROTOCOL.md).

Do not assume that borrowing these details reproduces the benchmark. Dynamic voting delegates, shared lineage allowances, night-only family channels, fixed original-body role semantics and the single simultaneous ballot per day are changes to the task.

## Upstream code and attribution

The curated role machinery draws on Dwarkesh Patel's MIT-licensed [`clocktower`](https://github.com/dwarkeshsp/clocktower) implementation, inspected at commit `c5d9696b42212f4bd4a99c0b5de831fa0643110b`. Preserve the vendored license and attribution when redistributing the copied role-state and character files. The MIT license on that implementation does not grant ownership of the original game's trademarks or assets. [Upstream license](https://github.com/dwarkeshsp/clocktower/blob/main/LICENSE).

The research design follows the local **taste** skill: write predictions and alternatives before interpreting a run, preserve evidence that can overturn the explanation, and separate efficiency measurements from claims about learning or internal mechanisms. This release adds evaluation infrastructure, not an RL training implementation.
