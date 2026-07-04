# Architecture Decision Records

Each record captures a real decision made building Booppa: the context, the alternatives weighed, what was chosen, and the consequences (including the ones that hurt). These are the questions an interviewer or a new engineer will ask.

## Contents

- [ADR-0001: Blockchain anchoring for evidence integrity](#adr-0001-blockchain-anchoring-for-evidence-integrity)
- [ADR-0002: Two-layer tamper evidence](#adr-0002-two-layer-tamper-evidence)
- [ADR-0003: Celery on Redis for background work](#adr-0003-celery-on-redis-for-background-work)
- [ADR-0004: Synchronous SQLAlchemy](#adr-0004-synchronous-sqlalchemy)
- [ADR-0005: Modular monolith over microservices](#adr-0005-modular-monolith-over-microservices)
- [ADR-0006: Application-level PII encryption](#adr-0006-application-level-pii-encryption)
- [ADR-0007: Deferred RFP intake after checkout](#adr-0007-deferred-rfp-intake-after-checkout)

---

## ADR-0001: Blockchain anchoring for evidence integrity

**Context.** Booppa's product is trustworthy evidence. A compliance PDF that the issuer could quietly re-date or edit has no evidentiary weight to a third party.

**Problem.** How does a recipient verify a document existed, unchanged, at a point in time, without trusting Booppa's word or database?

**Alternatives.**
- A trusted internal timestamp column. Rejected: requires trusting the issuer, the exact thing under question.
- A commercial trusted-timestamping authority (RFC 3161). Viable, but centralised trust and per-stamp cost, and less legible to customers than "check it on a public chain."
- Anchor a SHA-256 hash on a public blockchain. Chosen.

**Decision.** Hash each finished document with SHA-256 and anchor the hash on Polygon via the `EvidenceAnchorV3` contract (`anchorHash(bytes32, string)`). Only the hash goes on-chain, never the document, so nothing confidential is published. Polygon (over Ethereum mainnet) for low, predictable gas; Amoy testnet as the cost-free default with a guarded mainnet path.

**Consequences.**
- Independent verification: anyone can check a hash on a block explorer.
- Anchoring costs gas and latency, so it runs in Celery, not in-request, and is idempotent on `report_id`; the contract also rejects re-anchoring a hash.
- Testnet default trades finality for zero cost; disclosed in the PDF notice. See [TRADEOFFS.md](TRADEOFFS.md).

---

## ADR-0002: Two-layer tamper evidence

**Context.** On-chain anchoring proves a *document* existed, but not the *sequence of actions* that produced it, and paying gas for every intermediate event would be prohibitive.

**Problem.** How to make the full audit trail tamper-evident, cheaply, while keeping the strong independent guarantee for the final artifact?

**Alternatives.**
- Anchor every event on-chain. Rejected: cost and latency scale with activity.
- Trust the database audit table. Rejected: an insider or a breach with write access rewrites it silently.
- Hash-chain events off-chain, anchor the final artifact on-chain. Chosen.

**Decision.** `AuditChainEvent` links each event to the previous via `hash_prev` (`"GENESIS"` at the root), so any silent edit breaks every downstream link. The final document's SHA-256 is anchored on-chain per ADR-0001.

**Consequences.**
- Cheap per-event integrity plus strong independent proof for the deliverable.
- The chain still lives in the same database; its guarantee is *detectability*, not prevention. Combined with the on-chain anchor, tampering is detectable end to end.

---

## ADR-0003: Celery on Redis for background work

**Context.** PDF generation, blockchain anchoring, and S3 uploads are slow or gas-bearing. Doing them in the request would block the API and couple user latency to third-party systems.

**Problem.** How to run heavy work reliably, on a schedule where needed, without a heavy operational footprint?

**Alternatives.**
- FastAPI `BackgroundTasks`. Rejected: in-process, dies with the worker, no retries, no scheduling.
- A dedicated queue (SQS) plus a separate scheduler. More moving parts than the team needed.
- Celery with Redis as both broker and result backend, beat embedded in the worker. Chosen.

**Decision.** Two queues: `reports` for blocking work, `default` for light side effects. Beat embedded via `-B`, so schedules fire only in the worker container (about 14 cron jobs, including GeBIZ sync every 30 minutes and monthly PDPA rescans). Redis doubles as broker and result backend.

**Consequences.**
- One infrastructure dependency (Redis) covers queueing, results, and caching.
- Retries and idempotency (anchoring keyed on `report_id`) make failures safe to replay.
- Redis as result backend is not durable storage; results are treated as ephemeral (the RFP kit is cached at `rfp_result:{session_id}` and re-derivable).

---

## ADR-0004: Synchronous SQLAlchemy

**Context.** FastAPI supports async, and `asyncpg` is in the dependency set, but the live engine is synchronous SQLAlchemy over psycopg2 with synchronous `Session` objects.

**Problem.** Async DB access or synchronous, given the workload?

**Alternatives.**
- Full async SQLAlchemy + asyncpg. Higher connection efficiency under high concurrent I/O, but a more error-prone programming model and a large refactor across 57 routers.
- Synchronous SQLAlchemy with a tuned pool, pushing slow work to Celery. Chosen.

**Decision.** Keep the request path synchronous and simple. Bound concurrency with a tuned pool (`pool_size`, `max_overflow`, `pool_pre_ping`, `pool_recycle`) and move anything slow out of the request into Celery, so request handlers do short, bounded database work.

**Consequences.**
- Simpler, more debuggable handler code; no async foot-guns mixing sync libraries.
- Concurrency is bounded by pool size times process count; scale is horizontal (more processes) rather than via async I/O. Honest downside recorded in [TRADEOFFS.md](TRADEOFFS.md). Because the heavy work is already in Celery, the sync request path rarely holds a connection long.

---

## ADR-0005: Modular monolith over microservices

**Context.** Broad domain (PDPA, CSP/AML, procurement, billing, blockchain), small team, tightly coupled transactions.

**Problem.** One deployable or many services?

**Alternatives.**
- Microservices per domain. Rejected: a single purchase spans billing, RFP intake, PDF, anchoring, and audit; splitting them creates a distributed transaction with no payoff at this scale.
- Modular monolith with strong internal seams. Chosen.

**Decision.** One FastAPI process plus one Celery worker, organised into `api/`, `services/`, `workers/`, `orchestrator/`, `billing/`, `core/`. Service modules are the natural future extraction points.

**Consequences.**
- Simple deploys, in-process calls, one transaction boundary.
- Discipline required to keep the seams clean; the versioned model layout and the `services/` boundary are how that discipline is enforced.

---

## ADR-0006: Application-level PII encryption

**Context.** The platform stores NRIC, passport, and nominator IDs for CSP/AML compliance.

**Problem.** Is S3/RDS disk encryption enough for sensitive PII under PDPA?

**Decision.** No. Encrypt these fields at the application layer with Fernet via a SQLAlchemy `TypeDecorator`, key held in AWS Secrets Manager, versioned for rotation. Plaintext exists only in the running process holding the key.

**Consequences.**
- A database read (insider or breach) yields ciphertext, not identities.
- The process and the key become the thing to protect; key rotation is supported via version IDs. Fernet is AES-128; a move to AES-256 or KMS envelope encryption would be a migration, not a redesign. See [SECURITY.md](SECURITY.md).

---

## ADR-0007: Deferred RFP intake after checkout

**Context.** RFP bundle products need a buyer's brief before the kit can be generated, but payment happens first.

**Problem.** How to reconcile "paid now" with "brief supplied later" without losing either signal or double-fulfilling?

**Decision.** On the webhook, create a `PendingRfpIntake` row (`status=pending`, `session_id`) and email the buyer an intake link. On submit, flip to `submitted` and queue `fulfill_rfp_task`; the worker caches the result at `rfp_result:{session_id}`. The verify endpoint resolves brief state **session-scoped first**, then falls back to the latest pending row.

**Consequences.**
- Clean separation of payment and fulfillment; the result page polls a stable contract.
- A sharp edge: loosening the lookup to "latest regardless of status" lets an older `submitted` row falsely satisfy a new session, stranding the UI on "Generating...". The session-scoped priority is load-bearing and documented in [ARCHITECTURE.md](ARCHITECTURE.md#stripe-purchase-to-fulfillment).
