"""
Platform retention clock — keep byte-identical in fs-judgepapers,
fs-protocolgenerator and fs-scoremodifier (infra/functions/platform_clock.py).

The platform competition registry (figureskatingtools-site, table
`competitions`, PK "COMPETITION" / RK <platform guid>) owns the deletion date
of every competition: created + 60 days, extended a week at a time from the
site. A tool record bound to a platform competition (PlatformId) is deleted
on that date, not on a clock of its own. Access is read-only, through the
Storage Table Data Reader grant to this Function App's identity on that one
table (site repo's shared-data-access.bicep).

Feature-off and failure semantics:
- PLATFORM_STORAGE_ACCOUNT unset -> get_platform_table_client() returns None
  and callers keep the tool's own clock (local dev, unbound deployments).
- Row not found -> read_platform_clock() returns None (local clock applies).
- Any other error -> PlatformClockUnavailable. The cleanup timer must then
  SKIP the record: never delete early because the platform was unreachable.
"""
import logging
import os
import re
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

PLATFORM_TABLE_DEFAULT = "competitions"
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class PlatformClockUnavailable(Exception):
    """The platform registry could not be read (network, RBAC, throttling)."""


def get_platform_table_client():
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        return None
    table_name = os.environ.get("PLATFORM_COMPETITIONS_TABLE") or PLATFORM_TABLE_DEFAULT
    return TableClient(
        endpoint=f"https://{account_name}.table.core.windows.net",
        table_name=table_name,
        credential=DefaultAzureCredential(),
    )


def parse_utc(value):
    """ISO-8601 string (with or without Z) or datetime -> aware UTC datetime, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_platform_clock(platform_id, table_client=None):
    """
    -> {"deletionDate": aware datetime | None, "status": str, "purged": bool}
       or None when the platform has no such competition / feature is off.
    Raises PlatformClockUnavailable on any other failure.
    """
    if not platform_id or not _GUID_RE.match(str(platform_id)):
        return None
    try:
        client = table_client or get_platform_table_client()
    except Exception as exc:  # credential / client construction
        raise PlatformClockUnavailable(str(type(exc).__name__)) from exc
    if client is None:
        return None
    try:
        entity = client.get_entity("COMPETITION", str(platform_id).lower())
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        logging.error("Platform clock read failed: %s", type(exc).__name__)
        raise PlatformClockUnavailable(str(type(exc).__name__)) from exc
    return {
        "deletionDate": parse_utc(entity.get("DeletionDate")),
        "status": str(entity.get("Status") or "active"),
        "purged": bool(entity.get("PurgedUtc")),
    }


def effective_deletion(local_deletion, clock):
    """The platform date when the platform has one, else the tool's local date (aware UTC or None)."""
    if clock and clock.get("deletionDate") is not None:
        return clock["deletionDate"]
    return parse_utc(local_deletion)


def platform_says_delete(clock, now):
    """True when the platform competition is purged/deleted or its deletion date has passed."""
    if not clock:
        return False
    if clock.get("purged") or clock.get("status") == "deleted":
        return True
    deletion = clock.get("deletionDate")
    return deletion is not None and deletion <= now


def to_iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
