# Time Travel Benchmark adaptation

Reference: https://github.com/trivikrama-madhusudhana/time-travel-benchmark

Reviewed revision: `b16588722a33db033e52e5313dfe8f876d6de4c5`.

The relevant implementation is `src/mcp-server.mjs`: a controller-owned MCP
gateway injects the cutoff, validates source timestamps, caches retrievals,
handles tool calls asynchronously, and records events. `src/schema.mjs` checks
that cited source URLs appeared in the run's own tool events.

TCS Prover adapts these patterns to its existing Python gateway and Codex CLI
runtime. The read-only trajectory auditor matches actual returned documents to
the same problem's broker events using URL identity, capture timestamp and
raw-body digest. It does not import the upstream football data or prediction
schema. See the accompanying MIT license for the upstream attribution.

The upstream search/fetch backend requires OpenReward credentials. Its shipped
GPT runner uses a direct Responses API loop after encountering cancelled MCP
calls in Codex. Neither dependency is required here: model work stays in Codex
CLI, and the default retrieval backend uses isolated Codex general-web URL
discovery and verified Wayback captures. Scholarly API discovery remains an
optional provider. The discovery model's URL suggestions are untrusted and
cannot authorize source content; every source still passes the archive gate.
This is an adaptation of its gateway and audit
design, not a claim to provide OpenReward's archive or historical search index.

TCS preserves each problem's exact exclusive cutoff, unlimited proof budget,
author-only retrieval, and fail-closed archive verification. It does not adopt
the upstream day-end cutoff, 25-tool-call budget, missing-date fetch allowance,
or live source content in historical mode. New ordinary jobs use native live
search; the Advanced web cutoff setting can disable web-search tools with `-1`
or enable historical access through an explicit controller policy.
