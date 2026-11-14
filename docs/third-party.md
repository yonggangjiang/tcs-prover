# Third-party notices

TCS Prover adapts the design of one outside project. Its license and the notes
on what was adapted follow. (The same two texts are kept, unchanged, in the
local checkout's `third_party/time-travel-benchmark/`, which is not published.)

## Time Travel Benchmark

### Adaptation notes

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
schema. See the MIT license below for the upstream attribution.

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

### License

The upstream project's MIT license, with its copyright and permission notice
word for word:

```text
MIT License

Copyright (c) 2026 Trivikrama Madhusudhana

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
