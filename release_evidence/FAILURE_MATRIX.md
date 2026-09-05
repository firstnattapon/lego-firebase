# Required failure-boundary matrix

`PASS_LOCAL` หมายถึง controlled local/emulator evidence ของ candidate นี้ ไม่ใช่ live broker proof.

| scenario family | status | evidence |
|---|---|---|
| DNA=0, threshold edges, qty rounds zero, READY BUY/SELL | PASS_LOCAL | characterization/time-aligned/main pipeline tests |
| genesis no fill, first-fill slippage, PASS after finalized E | PASS_LOCAL | execution-confirmed + cashflow-freeze tests |
| Preview reject, explicit zero fill, accepted-not-filled | PASS_LOCAL | order-delivery/main tests |
| place timeout/response lost/not-found visibility | PASS_LOCAL | no-retry + reconcile tests |
| duplicate scheduler/simultaneous worker/stale generation | PASS_LOCAL | slot transaction + concurrent worker tests |
| crash immediately before/after durable pre-place | PASS_LOCAL | final-review fence tests |
| partial repeated -> FILLED/CANCELLED/EXPIRED | PASS_LOCAL | cumulative/terminal lifecycle tests |
| missing/NaN/negative qty or price; cumulative regression | PASS_LOCAL | order normalization/realized math tests |
| holdings delayed/missing/wrong direction/external interference | PASS_LOCAL | holdings incident + dispatch re-decision tests; exact external conservation remains a documented limit |
| market/clock/DNA ordinal/runtime identity/semantics mismatch | PASS_LOCAL | clock, preflight, downgrade and identity tests |
| RTDB rules/integration | PASS_EMULATOR | 56/56 on Emulator 4.11.2 |
| transaction callback retry/contention | PASS_LOCAL | deterministic Fake RTDB interleavings; live latency/contention profile BLOCKED |
| one ledger succeeds/other fails; row patch/audit mirror interrupted | PASS_LOCAL | repair/idempotency tests |
| archive vs recovery/fence/audit update | PASS_LOCAL | new compare-delete race regression + archive suite |
| old replay after pruning/archive | PASS_LOCAL | row-finalized secondary fence tests |
| kill switch while submitted | PASS_LOCAL | stop-new-place path keeps reconciliation semantics |
| mixed revision rollback with unresolved intent | BLOCKED | needs isolated Gen2 revisions and operator drill |
| restore while broker state newer than DB | BLOCKED | needs isolated backup/restore + real broker reconciliation |
| real terminal fill/holdings/order-count proof | BLOCKED | Webull UAT credentials + explicit one-order authorization absent |

SDK transport audit: `preview`, reads and detail may use bounded transient retry; `place_market_order` calls SDK once with no wrapper retry. Unknown result remains fenced and is reconciled by stable `client_order_id`.
