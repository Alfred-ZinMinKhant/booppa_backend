#!/usr/bin/env python
"""Inspect (and optionally drain) the orphan default ``celery`` Redis queue.

Nothing consumes ``celery``: the ECS worker runs ``-Q fast_queue,heavy_queue``.
Messages land there when a task is published by a producer whose Celery config
predates ``task_default_queue="fast_queue"`` (``app/workers/celery_app.py``), so
the queue is a graveyard that only ever grows — 3,478 messages / ~6 MB of a
30 MB Redis tier when this script was written.

Read-only by default. Always prints the task-name histogram and the distinct
customer emails found in the payloads *before* touching anything, because
"is any real customer work stranded in here?" is the only question that decides
between replaying and dropping.

    python scripts/drain_orphan_celery_queue.py                  # report only
    python scripts/drain_orphan_celery_queue.py --replay-to fast_queue
    python scripts/drain_orphan_celery_queue.py --purge --yes

``--replay-to`` re-publishes every message onto a queue the worker consumes,
preserving the message body verbatim, then clears the source queue. Use it when
the backlog contains work that must still run. ``--purge`` just deletes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

DEFAULT_QUEUE = "celery"
CONSUMED_QUEUES = ("fast_queue", "heavy_queue")


def _client():
    url = os.environ.get("REDIS_URL")
    if not url:
        sys.exit("REDIS_URL is not set.")
    import redis

    return redis.from_url(url, socket_connect_timeout=10)


def _decode(raw):
    """Task name, and any customer email visible in the payload."""
    try:
        msg = json.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed message is still a message
        return None, None
    headers = msg.get("headers") or {}
    name = headers.get("task")
    # Celery v2 protocol keeps args/kwargs as reprs in the headers; the full
    # body is base64 further down. The repr is enough to spot a real customer.
    blob = f"{headers.get('argsrepr')} {headers.get('kwargsrepr')}"
    email = None
    for token in blob.replace("'", " ").replace(",", " ").split():
        if "@" in token and "." in token:
            email = token.strip("\"'{}[]()")
            break
    return name, email


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", default=DEFAULT_QUEUE, help="source queue (default: celery)")
    ap.add_argument("--replay-to", metavar="QUEUE", help=f"re-publish onto one of {CONSUMED_QUEUES}")
    ap.add_argument("--purge", action="store_true", help="delete the queue after reporting")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if args.replay_to and args.purge:
        sys.exit("--replay-to and --purge are mutually exclusive.")
    if args.replay_to and args.replay_to not in CONSUMED_QUEUES:
        sys.exit(f"--replay-to must be one of {CONSUMED_QUEUES} — nothing else is consumed.")

    r = _client()
    messages = r.lrange(args.queue, 0, -1)
    if not messages:
        print(f"{args.queue}: empty — nothing to do.")
        return 0

    tasks: Counter = Counter()
    emails: Counter = Counter()
    for raw in messages:
        name, email = _decode(raw)
        tasks[name or "<unparseable>"] += 1
        if email:
            emails[email] += 1

    print(f"{args.queue}: {len(messages)} messages\n")
    print("By task:")
    for name, count in tasks.most_common():
        print(f"  {count:>6}  {name}")
    print("\nBy customer email seen in payload:")
    for email, count in emails.most_common():
        print(f"  {count:>6}  {email}")
    print(
        "\nCheck the emails above before dropping anything: an address that is not "
        "an internal/test account means real paid work is stranded here."
    )

    if not (args.replay_to or args.purge):
        return 0

    action = f"replay {len(messages)} messages to {args.replay_to}" if args.replay_to else f"DELETE {args.queue}"
    if not args.yes:
        if input(f"\nProceed to {action}? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    if args.replay_to:
        # RPUSH preserves ordering relative to LRANGE's head-to-tail read.
        for raw in messages:
            r.rpush(args.replay_to, raw)
        print(f"Replayed {len(messages)} messages to {args.replay_to}.")

    r.delete(args.queue)
    print(f"Cleared {args.queue}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
