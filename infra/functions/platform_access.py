"""
Platform competition access — keep byte-identical in fs-judgepapers,
fs-protocolgenerator and fs-scoremodifier (infra/functions/platform_access.py).

The platform registry (figureskatingtools-site) decides who may see or change a
competition. Identity is never an e-mail: the router forwards the signed-in
principal's Entra tenant id and object id (x-forwarded-user-tid /
x-forwarded-user-oid), and the platform maps that identity to its own internal
user id, so the mapping can later gain a second identity (Entra External ID)
without touching any grant. Two platform tables hold no personal data and are
readable by the tools through Storage Table Data Reader grants scoped to each
table (site repo's shared-data-access.bicep):

- `useridentities`  PK "IDENTITY"           / RK "<tid>_<oid>" (lowercase) -> UserId
- `competitionaccess` PK <platform guid>     / RK <userId>             -> Role

Roles: "owner" > "write" > "read". An admin (router header
x-forwarded-user-admin: 1, trusted only behind the fail-closed proxy secret)
is treated as "owner" without any lookup.

Failure semantics (fail CLOSED — never grant because something was unreadable):
- Unknown identity, unknown competition or no grant -> role_for() returns None;
  callers answer 404 platform_not_found (existence is never revealed).
- PLATFORM_STORAGE_ACCOUNT unset, table missing or any other read error ->
  PlatformAccessUnavailable; callers answer 503 platform_unavailable.

Answers are cached in-process for CACHE_SECONDS, so a revoked share stops
working within that time.
"""
import logging
import os
import re
import threading
import time

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

PLATFORM_IDENTITIES_TABLE_DEFAULT = "useridentities"
PLATFORM_ACCESS_TABLE_DEFAULT = "competitionaccess"
IDENTITY_PARTITION = "IDENTITY"
CACHE_SECONDS = 30
ROLE_RANK = {"read": 1, "write": 2, "owner": 3}
_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_cache = {}
_cache_lock = threading.Lock()


class PlatformAccessUnavailable(Exception):
    """The platform access tables could not be read (config, network, RBAC, missing table)."""


def _guid(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if _GUID_RE.match(text) else None


def identity_key(tid, oid):
    """'<tid>_<oid>' (lowercase GUIDs), or None when either is not a GUID."""
    t, o = _guid(tid), _guid(oid)
    if t is None or o is None:
        return None
    return f"{t}_{o}"


def role_satisfies(role, need):
    """True when `role` ("read"/"write"/"owner"/None) is at least `need`."""
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK[need]


def _table_client(env_name, default):
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        raise PlatformAccessUnavailable("PLATFORM_STORAGE_ACCOUNT unset")
    return TableClient(
        endpoint=f"https://{account_name}.table.core.windows.net",
        table_name=os.environ.get(env_name) or default,
        credential=DefaultAzureCredential(),
    )


def get_identities_table_client():
    return _table_client("PLATFORM_IDENTITIES_TABLE", PLATFORM_IDENTITIES_TABLE_DEFAULT)


def get_access_table_client():
    return _table_client("PLATFORM_ACCESS_TABLE", PLATFORM_ACCESS_TABLE_DEFAULT)


def _get(client, partition, row):
    """Entity dict, None when the row is absent; PlatformAccessUnavailable otherwise."""
    try:
        return client.get_entity(partition, row)
    except ResourceNotFoundError as exc:
        if getattr(exc, "error_code", None) == "TableNotFound" or "TableNotFound" in str(exc):
            logging.error("Platform access table not found")
            raise PlatformAccessUnavailable("TableNotFound") from exc
        return None
    except Exception as exc:
        logging.error("Platform access read failed: %s", type(exc).__name__)
        raise PlatformAccessUnavailable(type(exc).__name__) from exc


def _cached(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return True, hit[1]
    return False, None


def _store(key, value):
    with _cache_lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[key] = (time.monotonic() + CACHE_SECONDS, value)


def clear_cache():
    with _cache_lock:
        _cache.clear()


def resolve_user_id(tid, oid, identities_client=None):
    """Platform user id for the identity, or None when the identity is unknown/invalid."""
    key = identity_key(tid, oid)
    if key is None:
        return None
    hit, value = _cached(("id", key))
    if hit:
        return value
    try:
        client = identities_client or get_identities_table_client()
    except PlatformAccessUnavailable:
        raise
    except Exception as exc:  # credential / client construction
        raise PlatformAccessUnavailable(type(exc).__name__) from exc
    entity = _get(client, IDENTITY_PARTITION, key)
    user_id = _guid(entity.get("UserId")) if entity else None
    if user_id is not None:
        # Unknown identities are not cached: the platform creates the user on
        # their first request, and a cached miss would hide that for a while.
        _store(("id", key), user_id)
    return user_id


def role_for(platform_id, tid, oid, is_admin=False, identities_client=None, access_client=None):
    """
    -> "owner" | "write" | "read" | None for the caller on that platform competition.
    Admins get "owner" without lookups. Raises PlatformAccessUnavailable on read failure.
    """
    guid = _guid(platform_id)
    if guid is None:
        return None
    if is_admin:
        return "owner"
    user_id = resolve_user_id(tid, oid, identities_client=identities_client)
    if user_id is None:
        return None
    hit, value = _cached(("role", guid, user_id))
    if hit:
        return value
    try:
        client = access_client or get_access_table_client()
    except PlatformAccessUnavailable:
        raise
    except Exception as exc:
        raise PlatformAccessUnavailable(type(exc).__name__) from exc
    entity = _get(client, guid, user_id)
    role = entity.get("Role") if entity else None
    role = role if role in ROLE_RANK else None
    _store(("role", guid, user_id), role)
    return role
