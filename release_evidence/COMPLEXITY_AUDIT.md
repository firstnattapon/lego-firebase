# Targeted complexity and hot-path audit

## Changes accepted

- Extracted decision, execution and archive orchestration into owner-specific services.
- Kept `main.py` as composition root/HTTP façade and legacy monkeypatch surface; 1,538 → 272 lines.
- Added archive claim + exact transactional delete because a controlled race proved unconditional delete could lose a recovery update.
- Raised the cryptography pin and patched only wheel metadata/RECORD; Webull runtime aggregate remains enforced by provenance tests.

## Changes deliberately rejected

- No universal service container or framework: dependency injection exists only at service boundaries needed by behavior tests.
- No queue/database/microservice replacement: existing outbox/fence/RTDB model already encodes the money invariant.
- No merger of audit mirror, realized repair and model repair helpers: similar shapes represent independent witnesses and failure boundaries.
- No removal of fresh quote/holdings/open-order reads: each is a safety witness, not redundant computation.
- No financial formula, 17-column, Firebase path, HTTP/env or persisted status rename.

## Dependency direction

```text
main -> decision_service / execution_service / archive_service
services -> existing domain, persistence and broker adapter modules
domain modules -X-> main/services/Firebase/broker
```

AST/import smoke and all legacy behavior tests pass. Production optimization remains blocked until numeric workload/latency/read-byte/call/cost budgets exist. Without those measurements, retaining current safety reads is the only justified A5 outcome.
