# Execution state transition registry

| from | to | owner | evidence / guard | retry action |
|---|---|---|---|---|
| committed READY row | PENDING_DISPATCH | decision service | atomic pending marker + deterministic run/client ID | repair materialization; never recompute identity |
| PENDING_DISPATCH | claimed | execution service | intent claim + chain owner/generation | stale owner cannot place |
| claimed | PLACING_UNKNOWN | execution service | fresh quote/re-decision + Preview + durable pre-place marker + chain fence | lookup same client ID only |
| PLACING_UNKNOWN | SUBMITTED/PARTIAL_* | execution service | broker detail/open-order identity | bounded reconcile; no blind place retry |
| PARTIAL_* | PARTIAL_* | realized ledger | monotonic cumulative qty/fee delta | repeated cumulative value is no-op |
| SUBMITTED/PARTIAL_* | AWAITING_FILL_CONFIRMATION | execution service | terminal positive fill but holdings witness not moved | bounded holdings recheck; fence stays |
| any broker-active | FILLED/CANCELLED/EXPIRED | broker normalization | terminal detail; positive fill uses cumulative final values | finalize both ledgers idempotently |
| unresolved | RECONCILE_ABANDONED | execution service | retry budget exhausted | needs_manual_check; fence stays |
| terminal fill | CASHFLOW_FINALIZE_ERROR / REALIZED_MATH_ERROR | ledger owner | broker fill exists but one ledger refused | admin repair; never re-place |
| unsent | EXPIRED_UNSENT/SUPPRESSED_*/NOT_PLACED | execution service | durable proof place attempt never started | chain fence may clear |
| terminal + both ledgers safe | archived | archive service | no manual/audit/fence; old timestamp; claim + exact compare-delete | retry is idempotent |

Money-critical happens-before:

```text
runtime/semantics -> pending recovery -> slot/DNA -> snapshot/holdings
-> atomic row+intent marker -> claim+generation+chain fence
-> fresh dispatch checks -> Preview -> durable pre-place -> one Place attempt
-> terminal broker evidence -> holdings witness -> model+realized ledgers
-> audit repair complete -> fence release -> archive eligibility
```
