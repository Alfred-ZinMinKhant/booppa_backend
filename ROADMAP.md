# Roadmap

Where the system is heading, grouped by the concern each item addresses. Items are drawn from the known trade-offs and residual risks documented in [SECURITY.md](SECURITY.md) and [TRADEOFFS.md](TRADEOFFS.md), not aspirational features.

## Security hardening

- **Asymmetric JWT signing.** Move from HS256 with one shared secret to RS256 or EdDSA, so verify-only services hold only a public key and a leaked verifier cannot mint tokens. Contained change, isolated to `app/core/auth.py` and the verifiers.
- **Envelope encryption for PII.** Evaluate AWS KMS envelope encryption (or AES-256) over the current Fernet AES-128, for defence in depth and centralised key policy. Migration, not redesign, thanks to the `TypeDecorator` boundary.
- **Retire the unversioned `/api` mount.** Migrate the frontend's polling contracts to `/api/v1`, then drop the alias, which also removes the split rate-limit accounting.

## Blockchain

- **Mainnet migration path.** Exercise the guarded `USE_MAINNET` path for enterprise clients who need production finality, with the existing safety checks (real contract address, signing key) as gates. Business-triggered by active Enterprise volume, not an engineering blocker.
- **Batch anchoring.** The `EvidenceAnchorV3` contract already exposes `anchorBatch`; use it to amortise gas when many documents finalise together.

## Scalability

- **Revisit async where it pays.** The synchronous engine is deliberate, but specific high-fan-out read paths could adopt async access if traffic shape justifies it. Measure first; the current model scales horizontally by process.
- **Result durability.** Redis is broker, result backend, and cache. For results that must survive a Redis flush, consider persisting the RFP kit reference in Postgres alongside the cache entry.

## Observability

- **Close the loop on OpenTelemetry and Prometheus.** The instrumentation is wired; the next step is dashboards and alerts on the paths that matter most: fulfillment success rate, anchoring latency and failures, webhook-to-fulfillment lag.
- **Fulfillment alerting.** Make `_alert_payment_fulfillment_issue` a first-class signal (paid-but-not-fulfilled is the highest-severity business failure) with paging, not just logging.

## Developer experience

- **Lint and format tooling.** No `ruff`/`black`/`isort` is configured today. Adopt one, with a config file, so style is enforced rather than assumed.
- **Test the Stripe paths.** Stripe-dependent tests auto-skip without a test key; wire `STRIPE_TEST_PRICE_IDS_JSON` into CI so the payment-to-fulfillment pipeline is exercised, not just the non-Stripe code paths.
