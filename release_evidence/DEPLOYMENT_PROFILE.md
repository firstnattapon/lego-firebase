# Frozen deployment profile and assumptions

- Scope: operator-managed single strategy chain/account per deployment.
- Region/runtime target: Gen2, Python 3.12, explicit isolated UAT project first.
- Mutation boundary: source blocks broker place outside Webull UAT; production smoke uses `AUTO_SUBMIT=false`.
- Identity: function runtime SA, Scheduler invoker SA, Firebase project/RTDB and Webull account must be environment-specific.
- Secrets: Secret Manager only; no credential values in manifest/log/evidence.
- Scheduler: no automatic retry; cadence faster than slot; decision/execution/archive separately invoked.
- Rollout: exact candidate hash and config fingerprint; no mixed-version money path; unresolved intent blocks rollback until reconcile policy is followed.

Required operator inputs still absent:

1. isolated UAT project/RTDB/region and deploy/IAM authority;
2. Webull UAT app/account/market entitlement, symbol/category/window and maximum notional;
3. explicit permission for exactly one reviewed UAT `client_order_id` Place;
4. approved workload ceiling, latency/read-byte/call/cost budgets;
5. numeric RPO/RTO/retention, alert route and accountable owner;
6. separate production project and read-only smoke authorization; production order remains out of scope.
