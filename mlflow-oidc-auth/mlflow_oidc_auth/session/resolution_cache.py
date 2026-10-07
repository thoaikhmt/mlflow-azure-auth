"""Short-TTL cache of resolved server-side sessions (performance).

Every request authenticated with the browser session runs one database statement —
``SqlAlchemyStore.resolve_auth_session`` — to check that the session row is live and to read the
user's admin/active flags and encrypted provider tokens. That is cheap once, but MLflow's
playground and evaluation surfaces issue bursts of requests, and each one paid it. Permission
decisions were already cached; session resolution was not, which is what made those pages feel
slow.

This module keeps the resolved row for a short, configurable window
(``OIDC_SESSION_CACHE_TTL_SECONDS``, default 30s — the same lifetime as the permission caches). A
hit skips the database statement entirely; the row's encrypted tokens ride along, so no second
read is needed either.

Invalidation keeps the security-relevant paths immediate where it can:

* logout and a single-session revoke delete exactly the affected entry, so they stay immediate;
* a silent token refresh drops the entry, so the next request re-resolves rather than serving a
  spent blob;
* bulk revocation — deactivating a user, deleting one, an administrator revoking all sessions —
  is not tracked per entry and takes effect within the TTL. That matches the permission cache and
  is the deliberate trade the cache makes; disable it (TTL ``0``) for immediate revocation.

Entries are keyed by the opaque session id and hold only data the request already had in hand.
"""

from __future__ import annotations

from typing import Optional

from mlflow_oidc_auth.cache import CacheBackend, get_cache_backend
from mlflow_oidc_auth.config import config
from mlflow_oidc_auth.logger import get_logger

logger = get_logger()

_cache: Optional[CacheBackend] = None


def session_resolution_cache() -> Optional[CacheBackend]:
    """The cache, or None when disabled (TTL ``0``). Created on first use."""
    global _cache
    if _cache is None:
        ttl = int(getattr(config, "OIDC_SESSION_CACHE_TTL_SECONDS", 30) or 0)
        if ttl <= 0:
            return None
        _cache = get_cache_backend(
            "auth-session",
            maxsize=max(1, int(getattr(config, "OIDC_SESSION_CACHE_MAX_SIZE", 4096) or 1)),
            ttl=ttl,
        )
    return _cache


def get_cached_session(session_id: str):
    """The cached ``ResolvedSession`` for ``session_id``, or None. Never raises."""
    cache = session_resolution_cache()
    if cache is None or not session_id:
        return None
    try:
        return cache.get(session_id)
    except Exception as e:
        logger.debug("Session cache read failed (%s); falling back to the database", type(e).__name__)
        return None


def cache_session(session_id: str, resolved) -> None:
    """Remember a resolved session until the TTL elapses. Never raises."""
    cache = session_resolution_cache()
    if cache is None or not session_id or resolved is None:
        return
    try:
        cache.set(session_id, resolved)
    except Exception as e:
        logger.debug("Session cache write failed (%s); the next request re-resolves", type(e).__name__)


def invalidate_session(session_id: Optional[str]) -> None:
    """Drop one session's entry, or every entry when ``session_id`` is None. Never raises."""
    cache = session_resolution_cache()
    if cache is None:
        return
    try:
        if session_id:
            cache.delete(session_id)
        else:
            cache.clear()
    except Exception as e:
        logger.debug("Session cache invalidation failed (%s); entries expire via TTL", type(e).__name__)
