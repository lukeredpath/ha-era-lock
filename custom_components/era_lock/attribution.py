"""Shared resolution of an unlock credential against the options roster.

Both the lock entity (for its ``changed_by`` reason) and the event entity (for
its attributes) resolve a (method, credential-id) to a label / HA user the same
way, so the lookup lives here.
"""
from __future__ import annotations


def as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def lookup(
    credentials: list | None, method: str | None, cred,
) -> tuple[str | None, str | None, str | None]:
    """Find a configured credential by (method, id).

    Returns ``(label, user_id, user_name)`` with blanks normalised to ``None``,
    or ``(None, None, None)`` when there is no matching record.
    """
    cred_i = as_int(cred)
    if not method or cred_i is None:
        return None, None, None
    for c in credentials or []:
        if c.get("method") == method and as_int(c.get("id")) == cred_i:
            return (
                (c.get("label") or "").strip() or None,
                c.get("user_id") or None,
                c.get("user_name") or None,
            )
    return None, None, None


def actor(
    credentials: list | None, method: str | None, cred, who: str | None = None,
) -> str:
    """A human label for who/what unlocked: HA user > custom label > frida roster
    name (``who``) > raw ``method #id``."""
    label, _uid, name = lookup(credentials, method, cred)
    if name:
        return name
    if label:
        return label
    if who:
        return who
    cred_i = as_int(cred)
    if method:
        return f"{method} #{cred_i}" if cred_i is not None else method
    return "unknown"
