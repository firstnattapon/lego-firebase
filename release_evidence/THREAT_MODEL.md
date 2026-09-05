# Threat model and controls

| threat | control | residual / required proof |
|---|---|---|
| duplicate external order | deterministic client ID, per-intent claim, per-chain generation fence, no Place retry | real UAT broker order-count proof |
| stale worker after lease expiry | token+generation revalidation immediately before Place | isolated contention test at deployed latency |
| accepted response lost | durable pre-place ambiguity, lookup same ID, no availability-driven fence clear | real provider eventual-visibility timing |
| fabricated fill/accounting | terminal cumulative broker evidence + holdings direction witness + idempotent ledger transactions | exact conservation limited under manual/external trades |
| identity/config cross-talk | account/environment fingerprint + chain/config hash + fail closed | deployment isolation/IAM proof |
| semantics downgrade/mixed revision | monotonic semantics guard, candidate/config manifest | rollout/rollback drill |
| secret leakage | Secret Manager design, redaction functions/tests, ignore rules | CI gitleaks + deployed log review |
| public database mutation | default-deny RTDB rules; Admin SDK runtime only | deployed rules/IAM verification |
| archive deletes recovery evidence | ineligible flags/fence plus claim + transactional exact-delete | read-byte scaling remains budget-blocked |
| vulnerable dependency | exact pins, vendored wheel provenance, pip-audit CI gate | re-audit every candidate/advisory change |

No claim of regulatory certification, universal exactly-once network delivery, zero bugs, tenant isolation or 100% uptime is made.
