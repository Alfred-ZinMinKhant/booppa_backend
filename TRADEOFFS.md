# Trade-offs

Every engineering decision buys something and gives something up. These are the compromises in Booppa that are worth defending out loud, with the cost each one carries.

## Testnet anchoring by default

**Chosen:** anchor on Polygon Amoy testnet unless `USE_MAINNET=true`.

**Bought:** zero gas cost, so anchoring is available to every customer and every document, not rationed by budget. Proof-of-existence and tamper detection work identically to mainnet.

**Gave up:** the finality guarantees of a production chain. Amoy is a public test network; it can, in principle, be reset. The system is honest about this: the generated PDF carries a notice, and `config.py` documents the migration path. A guarded mainnet mode exists, with a safety check that refuses to silently anchor to the null address if it is enabled without a real contract and signing key.

**Why it is defensible:** the product value (independent, checkable proof that a document existed unaltered) is delivered on either network. Mainnet becomes worth its recurring cost at a scale of paying enterprise clients, which is a business trigger, not an engineering blocker.

## Synchronous database access

**Chosen:** synchronous SQLAlchemy over psycopg2, despite `asyncpg` being available and FastAPI being async-capable.

**Bought:** a simple, debuggable request path with no async/sync mixing hazards across 57 routers, and heavy work already pushed to Celery so handlers do short database work.

**Gave up:** the connection efficiency of async I/O under very high concurrency. Concurrency is bounded by pool size times process count, and the system scales by adding processes rather than by non-blocking I/O.

**Why it is defensible:** the slow, blocking, third-party-bound work (PDF, blockchain, S3) is in Celery, so the synchronous request path is short-lived. The complexity cost of a full async migration is not justified by the current traffic shape. It is a known, reversible decision (see [ADR-0004](ADR.md#adr-0004-synchronous-sqlalchemy)).

## Single symmetric JWT secret (HS256)

**Chosen:** all token types signed with one `SECRET_KEY` using HS256.

**Bought:** trivially simple signing and verification, one secret to manage.

**Gave up:** blast-radius containment. Whoever holds the secret can mint any token, including `admin`. An asymmetric scheme would let verify-only services hold only the public key.

**Why it is documented, not hidden:** at current scale one service both signs and verifies, so symmetric keys are adequate; the upgrade to RS256/EdDSA is on the roadmap and is a contained change (see [SECURITY.md](SECURITY.md)).

## Dual `/api` and `/api/v1` mount

**Chosen:** mount the same router at both prefixes.

**Bought:** the Next.js frontend's live polling contracts keep working against the unversioned surface while new work adopts `/api/v1`, with no coordinated cutover.

**Gave up:** clean rate-limit accounting. slowapi keys buckets by path, so the two prefixes count separately and the effective limit for a mixed caller is higher than nominal. The mount is documented in code as an intentional alias with a "do not remove without migrating" warning.

**Why it is defensible:** it is an explicit compatibility decision with a named migration path, not an accident, and the endpoints that depend on it are enumerated.

## Versioned model modules instead of domain packages

**Chosen:** group tables by product rollout (`models_v6` through `models_v13`) rather than by domain.

**Bought:** each rollout's schema and migration are self-contained and reviewable, and Alembic sees all 105 tables from a single tail import.

**Gave up:** domain locality. Related tables can live in different version modules, so navigation costs more.

**Why it is defensible:** the migration-safety and review benefits matter more for a compliance system where schema changes are audited, and the `__tablename__` convention keeps tables findable regardless of module.

## Modular monolith

**Chosen:** one deployable, strong internal seams, over microservices.

**Bought:** in-process calls, one transaction boundary, simple operations for a small team.

**Gave up:** independent per-domain scaling and deploy cadence.

**Why it is defensible:** transactions span domains (a purchase touches billing, intake, PDF, anchoring, audit), so a distributed split would add a hard consistency problem for no operational gain at this scale. The service modules are the extraction points if that changes (see [ADR-0005](ADR.md#adr-0005-modular-monolith-over-microservices)).
