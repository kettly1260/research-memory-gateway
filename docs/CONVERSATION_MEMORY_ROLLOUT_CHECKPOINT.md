# Conversation Memory Rollout Checkpoint

## Phase R0: Baseline Freeze
- repo root: G:\LLM\memory
- branch: v2/p0-hardening
- HEAD: ba50e6296fb871468d868eda405c6855bfee3353
- git status: clean on tracking, test scratch intact
- Python version: Python 3.12.0
- raw ZIP SHA-256: E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E (PASS)
- pytest: 175 passed, 3 warnings in 28.48s (PASS)
- git diff --check: exit code 0 (PASS)

## Phase R1: Runtime Config Draft
- config path: config.rollout.yaml
- resolved staging root: G:\LLM\memory\exports\conversation-staging\full-322-lexical
- resolved index path: G:\LLM\memory\exports\conversation-staging\full-322-lexical\.ai-memory\index.sqlite
- archive-local manifest path: G:\LLM\memory\exports\conversation-staging\full-322-lexical\.ai-memory\manifest.sqlite
- embedding_enabled: False
- vault_root: None
- allowlist roots: ['D:/Partition/F/Study/博士文件']

## Phase R2: 322-Session Lexical-Only Staging Import
- Round 1: 322 written, 0 skipped, 0 conflict, 0 failed_retryable, 0 index_stale (PASS)
- Round 2: 0 written, 322 skipped_unchanged (100% idempotent PASS)
- Stats: 87 user, 211 subagent, 24 guardian; 229 with parent_thread_id; 10 with attachments; 200 with tools; 313 with source anchors.

## Phase R3: 322-Session Lexical Index & Chunk Health
- Index Round 1: 322 indexed, 26,100 chunks (PASS)
- Index Round 2: 322 skipped_unchanged, 0 indexed (100% idempotent PASS)
- Chunk Health: min 2, mean 843.74, median 1039, p90 1446, p99 1496, max 1500; sections > 1500 = 0 (Hard cap PASS)
- Manifest Closure: 322/322 non-empty indexed_at, source_hash, section_hashes (0 anomalies PASS)

## Phase R4: Attachment Inventory Audit
- 3,471 records (1,374 found, 1,824 unresolved outside allowlist, 215 missing, 23 embedded, 35 remote)
- 0 hash leaks outside allowlist (PASS)
- Synthetic temp fixture lifecycle: found A -> content changed B -> file deleted (100% PASS)

## Phase R5: Retrieval QA & Recall Budget
- 5 fixed regression queries + 22 stratified queries: 100% accurate top & first anchored results (PASS)
- Recall budget: 10, 100, 1000, 1500 tokens: 100% within hard character cap (PASS)

## Phase R6: Manual Spot-Check
- 30 sessions sampled across 5 categories
- 10 sessions traced to raw ZIP entries/ordinals (100% PASS)
- P0=0, P1=0, P2=0, P3=0 (PASS)

## GATE-A: Lexical-only Staging Sign-Off
- STATUS: PASS (READY)

## Phase R7: Formal Canonical Vault Runtime Config Confirmation
- config path: G:\LLM\memory\config.conversation-production.yaml
- vault_root: D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault (exists, 12 child entries)
- canonical_subdir: 90_System/AI-Memory
- canonical root resolved path: D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault\90_System\AI-Memory
- archive-local manifest path: D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault\90_System\AI-Memory\.ai-memory\manifest.sqlite
- allowlist roots: ['D:/Partition/F/Study/博士文件']
- Security checks:
  - --vault without --confirm-vault fails (PASS)
  - --staging-dir pointing to vault fails (PASS)
- STATUS: PASS

## Phase R8: Canonical Vault Canary Import
- 5 Fixed Regression Sessions:
  - 019e3ad1-05d6-7382-972f-0d377e6092c6 (PASS)
  - 019eab7a-3a54-70b1-afd2-b89c0c98e8b2 (PASS)
  - 01a08fdc-6da5-7f93-9119-ff79d9fea710 (PASS)
  - 01a01289-e1b4-7242-98d9-368a75a16194 (PASS)
  - 019ea2ad-9a74-75c2-bf10-4246ca00ab25 (PASS)
- 5 Representative Sessions (Approved Option A):
  - 019e3ba5 -> 019e3ba5-49d0-7340-9dee-ae6d62f9eaf1 (ordinary user thread, PASS)
  - 019ea610 -> 019ea610-6534-7992-8ef1-343aebf7682a (parent/subagent lineage + long title, PASS)
  - 01a09144 -> 01a09144-1563-7053-91fc-4f8f2b824929 (attachment-heavy, PASS)
  - 019f7401 -> 019f7401-5e87-75e1-af7a-fc909a3f4df6 (tool-heavy, 332 tools, PASS)
  - 019ea53b -> 019ea53b-63ed-7062-8cc4-30395b46bbe2 (long / compaction, PASS)
- Dry-Run on 10 confirmed sessions: 10 dry_run, 0 collisions with existing Vault notes (PASS)
- Round 1 Import: 10 written into Vault/90_System/AI-Memory/Conversations/2026/ (PASS)
- Round 2 Import: 10 skipped_unchanged (100% idempotent PASS)
- Non-System Vault modification check: 0 files modified outside 90_System (PASS)
- FTS Index & Search: 905 chunks indexed, search verified (PASS)
- Obsidian UI spot-check: 5 sessions reviewed (PASS)

## GATE-B: Canonical Vault Sign-Off
- STATUS: PASS (READY)

## Phase R9: Embedding Production Config & NAS Smoke
- Config updated: config.conversation-production.yaml (retrieval.mode=hybrid, embedding.enabled=true, base_url=http://192.168.22.102:28001/v1, model=bge-m3, timeout=30s, max_retries=2)
- ZeroTier TCP port 28001: TcpTestSucceeded=True (PASS)
- NAS Smoke test: python scripts/smoke_bge_m3.py -> PASS (dimension=1024, exit code=0)
- STATUS: PASS

## Phase R10: BGE-M3 Small-Batch Canary
- Fix candidate URLs: Prevented redundant /v1/v1/embeddings when base_url ends with /v1 (PASS)
- Fix embedding accounting (Section 16.1): `embedded_chunks` distinguishes actual new embedding requests from cache reused and failures; added `cache_reused_chunks` and `failed_chunks` (PASS)
- Unit test suite: 176 passed, 3 warnings in 29.35s (PASS)
- 5 Sessions Initial Embedding:
  - Scanned: 5, Indexed: 5, Chunks: 173, Unique Identities: 153
  - Actual new embedding requests: 74 + 79 = 153 (100% match)
  - Failures: 0, Dimension mismatches: 0, Dimensions: 1024
- Same-batch immediate rerun:
  - With --changed-only: 5 skipped_unchanged, 0 new calls (PASS)
  - Force re-index: 100% cache hits (stats_cache_reused=count, stats_new_embeddings=0) (PASS)
- Synthetic incremental lifecycle (Section 16.2):
  - Initial index: 4 new calls (PASS)
  - Immediate rerun: 0 new calls, 4 cached (PASS)
  - 1 chunk modified: 1 new call, 3 cached (PASS)
  - v2 bump: 4 new calls (PASS)
  - v1 rollback: 0 new calls, 4 cached, live identity restored (PASS)
  - Active embedding binding verified (no mixed active versions) (PASS)
- Hybrid/Vector search verification:
  - Vector search for 'Nihao': top score 0.6962 on 019e3ad1 (PASS)
  - CLI hybrid search: returned combined lexical + vector scores accurately (PASS)
- Raw export ZIP SHA-256: E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E (UNTOUCHED PASS)

## GATE-C: Bulk BGE-M3 Sign-Off
- STATUS: PASS (READY)

## Phase R11: Formal Rollout - Batch 1 (10 Sessions Import & Embedding)
- Scope: 10 Canonical Sessions (`019e3ad1`, `019eab7a`, `01a08fdc`, `01a01289`, `019ea2ad`, `019e3ba5`, `019ea610`, `01a09144`, `019f7401`, `019ea53b`)
- Canonical Vault Import: 10 written, 0 conflicts, 0 failures (PASS)
- BGE-M3 Embedding Index:
  - Round 1 (Canary 5 sessions): 153 new embeddings, 20 reused, 0 failures (PASS)
  - Round 2 (Remaining 5 sessions): 870 new embeddings, 26 reused, 0 failures (PASS)
  - Total Chunks Indexed: 1,069 chunks
  - Total Unique Vector Identities in DB: 1,023 / 1,023 (100% COMPLETE)
  - Failures / Dimension Mismatches: 0 / 0
  - Dimensions: 1024
- Hybrid & Vector Search Validation:
  - 'Nihao' -> Top 1 hybrid score 0.3992 (PASS)
  - '迁移' -> Top 1 vector score 0.5352 on `019f7401` (PASS)
- Raw export ZIP SHA-256: E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E (UNTOUCHED PASS)
- STATUS: BATCH 1 PASS (10/10 READY).

## Phase R11: Formal Rollout - Batch 2 (25 Sessions Import & Embedding)
- Scope: 25 Canonical Sessions (sessions 11~35 from export)
- Canonical Vault Import:
  - Dry-run: 25 dry_run, 0 collisions (`exports/batch-2-import-dry.json`) (PASS)
  - Real Import: 25 written into `Vault/90_System/AI-Memory/Conversations/2026/` (PASS)
  - Idempotency Test: 25 skipped unchanged, 0 written (100% idempotent PASS)
  - Vault Files Count: 35 total notes under `Vault/90_System/AI-Memory/Conversations/2026/`
  - Non-System Vault modification check: 0 files modified outside 90_System (PASS)
- BGE-M3 Embedding Index:
  - Scanned files: 35
  - Skipped files: 10 unchanged (Batch 1 notes safely skipped)
  - Indexed files: 25 new notes
  - Chunks indexed: 679 chunks
  - New embedding requests to NAS: 530 calls (PASS)
  - Cache reused chunks: 149 chunks (PASS)
  - Embedding failures: 0 (PASS)
  - Dimension mismatches: 0 (all 1024-dim) (PASS)
  - Total Chunks across 35 sessions in DB: 1,748 chunks
  - Total Unique Vector Identities in DB: 1,553
  - Sections without embeddings: 0 (100% complete coverage)
- Raw export ZIP SHA-256: E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E (UNTOUCHED PASS)
- STATUS: BATCH 2 PASS (25/25, Cumulative 35/35 READY). Ready for Batch 3 (50 sessions).

