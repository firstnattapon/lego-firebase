# Acceptance report

Candidate working-tree hash: `eda031662952858ac16bfbc727c63057a59e43d8fa57ab27071b08a24cb42499`
Status: **NOT_READY**
Required: 76 · PASS: 44 · BLOCKED: 32 · pass rate: 57.8947%

คะแนนนี้ใช้ registry v1 จำนวน 76 criteria จึงห้ามเทียบตรงกับรายงานเก่า 80/100 ที่ใช้ weighting คนละชุด

## Blocked criteria

- `G03-C09` chain/account deployment isolation scope: ต้องมี isolated deployment และ policy เมื่อหลาย chain/account แชร์ buying power/symbol
- `G04-C06` mixed-version behavior/rollback: ต้องรัน mixed-version rollout/rollback drill กับ unresolved intent
- `G05-C04` retention vs dedupe/replay horizon: ยังไม่มี operator-approved retention และ replay/dedupe horizon
- `G05-C05` scan growth measured/bounded within supported workload: ยังไม่มี workload ceiling และ deployed RTDB read-byte measurement
- `G06-C04` real RTDB concurrency/integration evidence: มี emulator rules แต่ยังไม่มี real RTDB transaction contention evidence
- `G06-C06` no unrelated user-change overwrite: source delivery ไม่มี .git จึงพิสูจน์ unrelated working-tree changes ไม่ได้
- `G07-C01` authenticated least-privilege invoker/admin: ไม่มี cloud IAM/deploy authority สำหรับ least-privilege verification
- `G07-C02` runtime identity/isolation verified: ไม่มี isolated deployed runtime identity evidence
- `G07-C03` RTDB access model verified: ยังไม่ได้ verify deployed RTDB IAM/rules model
- `G08-C01` reproducible exact candidate build: ต้องสร้าง clean isolated Gen2 build จาก exact candidate
- `G08-C02` actual source deployed in isolated env: ไม่มี GCP project/deploy authorization
- `G08-C03` three entrypoints invoked: ไม่มี deployed URLs สำหรับ invoke 3 entrypoints
- `G08-C04` env and SDK compatibility: ต้องพิสูจน์ env/SDK ใน deployed Gen2 runtime
- `G08-C05` logging/response/auth negative tests: ต้องทดสอบ deployed logging/response/auth negative paths
- `G09-C01` real authenticated snapshot: ไม่มี Webull UAT credentials/market entitlement ใน session
- `G09-C02` reviewed bounded UAT order: ขาด symbol/window/notional และ explicit one-order approval
- `G09-C03` real preview/place/status: ห้าม Place โดยไม่มี explicit UAT order authorization
- `G09-C04` terminal positive fill observed: ยังไม่มี real terminal positive fill
- `G09-C05` post-fill holdings witness: ยังไม่มี post-fill holdings witness จริง
- `G09-C06` both ledgers correct once: ยังไม่มี live fill เพื่อพิสูจน์ two-ledger once-only
- `G09-C07` replay/reconcile broker duplicate-count proof: ยังไม่มี broker order-count evidence หลัง replay/reconcile
- `G10-C02` unresolved-age/alert routing drill: ยังไม่มี deployed alert route/owner drill
- `G10-C04` restore with external broker reconciliation: ต้องใช้ isolated backup restore และ external broker reconciliation
- `G10-C05` rollback while unresolved: ต้องรัน rollback drill ขณะมี unresolved intent
- `G10-C06` numeric RPO/RTO/retention/owner recorded: operator ยังไม่กำหนด numeric RPO/RTO/retention/owner
- `G11-C03` locked numeric budgets/workload ceiling: operator ยังไม่ล็อก numeric budgets/workload ceiling
- `G11-C04` latency/calls/read-bytes regression within budget: ยังไม่มี deployed latency/calls/read-byte regression
- `G11-C05` backlog/soak sustained capacity: ยังไม่มี soak/backlog sustained-capacity run
- `G11-C06` cost estimate reproducible: ยังไม่มี reproducible deployed cost profile
- `G12-C03` all required criteria PASS without exclusions after failure: ยังมี required criteria BLOCKED
- `G12-C04` zero unresolved critical/high defects in scope: external deployment/UAT/operations high-risk gates ยังไม่ปิด
- `G12-C06` independent operator can follow deployment/recovery instructions: ยังไม่มี independent operator deployment/recovery walkthrough
