"""Framework durable return facade v1. Routing choice remains with the caller.

Uses the legacy module's *same* functions and thread-local lock custody. There
is no provider import, resolver invocation or filesystem write on import. The
legacy return_route module and its existing public names remain available.
"""
from return_route import (
    acknowledge_inbox_drop, atomic_json, canonical, event_for, immutable_json,
    persist_event, inbox_publication_barrier,
    publication_roots, publish_event, publisher_barriers, release_reservation,
    reserve_route, sha256, sync_dir, validate_route,
)

API = "framework-return/1"
__all__ = (
    "API", "acknowledge_inbox_drop", "atomic_json", "canonical", "event_for",
    "immutable_json", "persist_event", "inbox_publication_barrier", "publication_roots", "publish_event",
    "publisher_barriers", "release_reservation", "reserve_route", "sha256",
    "sync_dir", "validate_route",
)
