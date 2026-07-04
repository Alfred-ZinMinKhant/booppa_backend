# Security

Booppa is a compliance platform, so its own security posture is part of the product. This document describes the controls in place, the threat model, and, deliberately, the residual risks. Hiding known weaknesses would be the wrong thing for a system whose whole value is trustworthy evidence.

## Contents

- [Authentication](#authentication)
- [Authorization and tenancy](#authorization-and-tenancy)
- [PII encryption](#pii-encryption-at-rest)
- [Tamper evidence](#tamper-evidence)
- [Transport and network](#transport-and-network)
- [Secrets management](#secrets-management)
- [Rate limiting and abuse](#rate-limiting-and-abuse)
- [Threat model](#threat-model)
- [Known residual risks](#known-residual-risks)

## Authentication

Two credential types converge on one dependency (`app/core/db.py:get_current_user`):

**JWT.** `app/core/auth.py` mints HS256 tokens signed with a single `SECRET_KEY`. Every token carries a `type` claim so it cannot be replayed out of context:

| Type | Lifetime | Purpose |
|------|----------|---------|
| `access` | 24h | authenticated API calls |
| `refresh` | 30d | mint new access tokens |
| `admin` | 12h | admin endpoints |
| `password_reset` | 30min | one-shot reset flow |

Verification decodes with the matching verifier and checks the `type`, so a refresh or reset token presented to an access-protected route is rejected.

**API keys.** Integrators use keys prefixed `bp_`. Keys are stored only as SHA-256 hashes, checked for `revoked_at IS NULL`, and stamped with `last_used_at`. A leaked database row does not reveal a usable key.

Passwords are bcrypt-hashed via passlib. The application refuses to boot in production if `SECRET_KEY` is still the default:

```python
if settings.ENVIRONMENT == "production" and settings.SECRET_KEY == "change-me-in-production":
    raise RuntimeError("FATAL: SECRET_KEY is still the default value...")
```

## Authorization and tenancy

Multi-subsidiary tenancy is modelled with `User.parent_user_id`, a nullable self-FK. A parent org and its subsidiaries form one tenancy boundary. The security-relevant consequence: any query that enforces isolation must account for the parent/child relationship, not filter on `user_id` alone. This is called out in [ARCHITECTURE.md](ARCHITECTURE.md#the-model-layer-versioned-not-domain-split) because it is an easy place to introduce a horizontal access bug.

## PII encryption at rest

Sensitive identifiers (NRIC, passport numbers, nominator IDs) are encrypted at the application layer, not left to disk encryption alone. `app/core/encryption.py` uses Fernet (AES-128-CBC with an HMAC-SHA256 authentication tag) through a SQLAlchemy `TypeDecorator`, so encryption and decryption are transparent to the model code.

- The key is loaded once from **AWS Secrets Manager** in production and cached in memory; it is never placed in an env var or in code.
- Key versioning (`CSP_PII_KEY_VERSION`) supports rotation.
- A local dev fallback key exists and logs a warning that it is non-production.

The rationale, stated in the module itself, is PDPA s.24: disk-level encryption on S3/RDS does not by itself satisfy the duty of care for sensitive personal data processed by a compliance platform, because anyone with database or storage access sees plaintext. Application-level encryption narrows the plaintext exposure to the running process holding the key.

## Tamper evidence

Two independent layers (detailed in [ARCHITECTURE.md](ARCHITECTURE.md#tamper-evidence)):

1. **On-chain SHA-256 anchoring** on Polygon via `EvidenceAnchorV3`, giving independent proof-of-existence that does not rely on trusting Booppa's database.
2. **Off-chain hash chain** (`AuditChainEvent`, `hash_prev` linking back to `GENESIS`) giving per-event sequence integrity.

An attacker with write access to the database cannot silently rewrite history: editing one audit event breaks the chain, and the anchored hash of the final document will no longer match a tampered document.

## Transport and network

The backend is not exposed directly. It sits behind a **Cloudflare Tunnel** running in ECS Fargate, so there is no public inbound origin IP to attack. CORS origins are explicit (`ALLOWED_ORIGINS`, default `http://localhost:3000`), never a wildcard, and credentials are allowed only for those origins.

## Secrets management

- PII key: AWS Secrets Manager.
- Signing keys, Stripe keys, provider keys: environment injected from the ECS task definition's secret references (verified: `infra/terraform/exec_secrets.tf` holds ARNs and references, no literal secret values).
- A mainnet safety guard logs loud warnings if `USE_MAINNET=true` without a real contract address or signing key, so a misconfiguration cannot silently burn MATIC anchoring to the null address.

## Rate limiting and abuse

slowapi applies a default limit of 200 requests/minute keyed by remote address. See the residual-risk note below about how the dual mount interacts with this.

## Threat model

| Actor | Concern | Control |
|-------|---------|---------|
| Anonymous internet | direct attack on origin | Cloudflare Tunnel, no public origin; rate limiting |
| Authenticated tenant | access another tenant's data | tenancy boundary via `parent_user_id`; per-user scoping |
| Leaked API key | impersonation | keys stored hashed; revocation via `revoked_at` |
| DB read access (insider / breach) | read sensitive PII | Fernet application-level encryption; key not in DB |
| DB write access | rewrite evidence history | hash chain + on-chain anchor |
| Recipient of a document | trust a forged report | independent on-chain hash verification |
| Operator misconfiguration | burn mainnet gas / boot insecure | boot guard on default SECRET_KEY; mainnet guard |

## Known residual risks

Stated plainly, because interviewers and auditors will find them anyway:

- **HS256 with a single shared secret.** All token types are signed with one symmetric `SECRET_KEY`. Anyone who obtains that secret can mint any token, including `admin`. An asymmetric scheme (RS256/EdDSA) with a private signer and public verifiers would shrink the blast radius and allow verify-only services. This is a deliberate simplicity trade-off for the current scale, tracked in [ROADMAP.md](ROADMAP.md).
- **Dual `/api` and `/api/v1` mount.** Because slowapi keys buckets by path, the same logical endpoint has two independent rate-limit counters, so the effective limit for a caller who mixes prefixes is higher than the nominal 200/min. The mount is retained for frontend compatibility; the fix is to migrate callers and drop the alias.
- **Testnet finality.** Anchoring defaults to Polygon Amoy testnet, which gives proof-of-existence but not the finality guarantees of mainnet. This is disclosed to customers in the generated PDF notice, and a guarded mainnet path exists for when it is warranted (see [TRADEOFFS.md](TRADEOFFS.md)).
- **AES-128 via Fernet.** Fernet is AES-128-CBC. It is a sound, well-reviewed default; a future requirement for AES-256 or envelope encryption with AWS KMS would be a migration, not a redesign.
