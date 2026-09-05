# Operations, recovery and rollback runbook

## Stop new money movement

1. Set `AUTO_SUBMIT=false` on decision and execution revisions; do not delete outbox/fence.
2. Keep `lego_order_worker` available for lookup/reconciliation of already fenced/submitted runs.
3. Verify response/config identity and confirm broker mutation count stays unchanged for new intents.

## Unresolved or manual terminal

1. Identify `chain_key`, `run_id/client_order_id`, runtime fingerprint and current dispatch generation.
2. Read broker order detail/open orders and holdings; record redacted raw evidence.
3. Run `lego_admin_reconcile.py` dry-run (default). It must never preview/place/cancel/replace.
4. Review recurrence/realized/row/audit witnesses and use the exact ACK only after evidence agrees.
5. Apply once, rerun dry-run, verify immutable admin audit and fence release predicate.

## Rollback

- Never shift traffic to a revision whose cashflow semantics or runtime identity is older/different.
- If any unresolved `inflight_run_id` exists, keep the current execution revision for reconciliation or prove the rollback revision understands the same status/schema.
- Roll back source/config together using manifest hashes; keep `AUTO_SUBMIT=false` until 3 entrypoints and identity checks pass.

## Restore

1. Restore into an isolated project, never over live RTDB first.
2. Disable new Place; compare restored unresolved intents with broker state newer than backup.
3. Repair through stable client IDs and admin reconciliation; verify both ledgers/audit/fence.
4. Promote only after no unresolved evidence is lost and operator signs the drill.

## Current drill status

Local logic paths are tested. Alert routing, isolated restore, mixed-revision rollback, numeric RPO/RTO and real broker reconciliation are **BLOCKED** pending the operator inputs in `DEPLOYMENT_PROFILE.md`.
