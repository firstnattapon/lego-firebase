# Reality audit — LEGO Firebase commercial release candidate

วันที่ตรวจ: 2026-09-05
นโยบาย: audit current workspace; reference commit `c3e5394…` เป็น historical evidence เท่านั้น
Git identity: **ไม่มี `.git` ใน source delivery นี้** จึงใช้ canonical working-tree hash ใน `RELEASE_MANIFEST.json` แทนและห้ามอ้าง SHA ปัจจุบัน

## Deployment profile ที่ล็อก

- operator-managed, single account/chain deployment; ไม่รับรอง multi-tenant isolation, billing หรือการรับเงินลูกค้า
- Webull `UAT` สำหรับ order mutation เท่านั้น; production ใน scope นี้เป็น read-only smoke และ `AUTO_SUBMIT=false`
- 3 Gen2 HTTP entrypoints: `lego_one_row`, `lego_order_worker`, `lego_archive_worker`
- Python 3.12; dependencies exact-pinned; Firebase RTDB เป็น persistent system of record

## Current-source mapping

| label | สิ่งที่พบ | หลักฐาน/การตัดสิน |
|---|---|---|
| ALREADY_IMPLEMENTED | 17-column model, execution-confirmed ledger, durable outbox, per-chain dispatch fence, runtime/semantics guards, admin reconcile | focused tests + full suite |
| SAME_INVARIANT_DIFFERENT_NAME | `main.py` เดิมมี Decision/Execution/Archive entrypoints แต่ยังไม่ได้แยก service ownership | extract โดยรักษา function bodies/call order |
| ACTUAL_GAP | archive copy/delete ไม่มี conditional delete หลัง eligibility read | แก้เป็น claim + compare-and-delete; race regression ผ่าน |
| ACTUAL_GAP | `cryptography==48.0.1` มี 5 advisories ณ วัน audit | pin 50.0.0, patch wheel metadata only, pip-audit ผ่าน |
| COMPLEXITY_HOTSPOT | `main.py` 1,538 บรรทัดรวม orchestration | หลัง extraction เหลือ 272 บรรทัด; service imports acyclic |
| POSSIBLE_DUPLICATION | audit/mirror/repair helpersคล้ายกันแต่เป็น independent safety witnesses | คงไว้; ไม่มี proof ว่ารวมแล้วปลอดภัยกว่า |
| PROTECTED_DO_NOT_TOUCH | P0/P_acted, E branch semantics, 17 columns, Firebase paths, run/client-order identity, money fence, HTTP/env behavior | characterization tests ผ่าน; refactor ไม่เปลี่ยนสูตร/status/path |
| NEEDS_CHARACTERIZATION | real chain/account isolation, mixed-version rollback, live holdings timing, archive scan bytes/cost | BLOCKED จนมี isolated deployment + workload profile |

## Fresh baseline

- Backend before targeted fixes: 643 passed, 1 skipped (15.90s)
- Backend after fixes/refactor/dependency update: 644 passed, 1 skipped (10.25s)
- Emulator-only rules suite: 56 passed; Firebase Database Emulator 4.11.2; Temurin 21.0.12.1
- Streamlit: 53 passed (5.99s fresh final run)
- compile/import/3-entrypoint smoke: PASS
- pip check: PASS
- pip-audit backend + Streamlit: 0 known vulnerabilities after remediation
- clean venv exact-requirements install: PASS; backend 644 passed, 1 skipped (8.81s), pip check/audit PASS

เวลา pytest ใช้เป็น local observation เท่านั้น ไม่ใช่ production latency budget เพราะ run ต่างเวลา/cache ต่างกัน

## Evidence gaps ที่ห้ามแปลงเป็น PASS

- ไม่มี gcloud/Firebase CLI installation เดิม, active cloud project, IAM/deploy authority หรือ runtime credentials ใน session
- ไม่มี Webull credentials/environment variables และไม่มี explicit authorization ให้ Place UAT order
- ไม่มี production authorization; A10 ห้ามเริ่มตามแปลน
- ไม่มี workload ceiling, numeric RPO/RTO/alert owner/cost budget ที่ operator อนุมัติ
- ไม่มี Git metadata จึงพิสูจน์ branch/HEAD/dirty state ไม่ได้จาก source delivery นี้

ดังนั้นสถานะที่ซื่อสัตย์คือ `NOT_READY`; รายละเอียด criterion-level อยู่ใน `ACCEPTANCE.json`.
