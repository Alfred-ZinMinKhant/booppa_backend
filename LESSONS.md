# Lessons

What building and operating Booppa taught, beyond the feature list. These are the things that would change how I approach the next system.

## Tamper evidence is a systems property, not a feature

The instinct is to reach for one strong mechanism: put it on the blockchain. But on-chain anchoring alone proves a document, not the process that made it, and paying gas per event is a non-starter. The durable insight was that integrity comes from *composing* a cheap per-event guarantee (an off-chain hash chain) with a strong independent one (the on-chain anchor of the final artifact). Neither is sufficient; together they make tampering detectable end to end. Reaching for one silver bullet would have been weaker and more expensive.

## Verify claims against source, not the README

The old README and even internal notes described the database layer as "async SQLAlchemy + asyncpg." The code is a synchronous engine over psycopg2. `asyncpg` sits in requirements unused. Documenting the system honestly meant reading `app/core/db.py`, not trusting prior prose. A backend that misdescribes its own concurrency model invites exactly the interview question it cannot answer. Docs drift; source does not lie.

## Push slow work out of the request, then simplicity is affordable

Choosing synchronous SQLAlchemy would be indefensible if PDF rendering, blockchain writes, and S3 uploads happened in the request. They do not; they are in Celery. Once the expensive, third-party-bound work is offloaded, the request path is short, and the simpler synchronous model stops being a liability. The lesson is ordering: fix where the slow work runs first, and the concurrency model becomes a lower-stakes choice.

## Payment and fulfillment are different events, and conflating them corrupts state

The RFP flow taught this the hard way. Payment happens at the webhook; the brief arrives later; the kit is generated later still. Modelling that as one step fails. Modelling it as a `PendingRfpIntake` state machine works, but only if the state lookup is scoped correctly. The regression where an older `submitted` row from a prior cycle satisfies a new session, freezing the UI on "Generating...", came directly from a lookup that was one `status` filter too loose. Session-scoped-first is load-bearing, and the fix lived in a single ordering decision.

## Silent failure is worse than loud failure

`email_service.send_html_email` returns `False` on provider rejection instead of raising. That is a reasonable choice, but it means a fulfillment flow can pay, generate, and never deliver, with nothing thrown. The lesson that shows up across the codebase: any operation whose failure matters must have its result *checked*, and fulfillment paths route failures through an explicit alert (`_alert_payment_fulfillment_issue`) rather than trusting exceptions to surface them.

## Guardrails belong at the boundary of expensive, irreversible actions

Mainnet anchoring costs real money and cannot be undone. So the config layer refuses to boot production with a default secret, and warns loudly if mainnet is enabled without a real contract or signing key, preventing a silent burn to the null address. The pattern generalises: the moments worth a hard guard are the ones that are expensive or irreversible, and those guards belong as close to the boundary as possible, at startup or at the call site, not buried in a runbook.

## A compliance product must hold itself to its own standard

Building a platform that judges other companies' data protection forced the question inward: is our own PII encrypted to the standard we sell? Disk encryption alone was not good enough, so PII moved to application-level Fernet with a Secrets Manager key. The broader lesson is that the product's premise sets the floor for its own engineering. A compliance tool that leaks is not just insecure, it is self-refuting.
