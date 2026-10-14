"""
Platform retention clock — keep byte-identical in fs-judgepapers,
fs-protocolgenerator and fs-scoremodifier (infra/functions/platform_clock.py).

The platform competition registry (figureskatingtools-site) owns the one
deletion date of every competition: created + 60 days, extended a week at a
time from the site. It mirrors the non-personal part of that state into its
own table `retentionclock` (PK "COMPETITION" / RK <platform guid, lowercase>;
columns DeletionDate, Status, PurgedUtc) and reconciles it nightly. Tools read
ONLY that table, through a Storage Table Data Reader grant scoped to it (site
repo's shared-data-access.bicep) — never the registry's `competitions` table,
which holds operators' e-mail addresses and FS Manager sender IPs.

A tool deletes a bound record's data only on the platform's verdict: the
competition is `Status == "deleted"` or has `PurgedUtc`. The date alone is
never enough — the platform decides (it may be in dry-run, or an overdue
competition may just have been extended). Tool timers run after the platform's
03:00 UTC sweep so they follow it the same night.

Failure semantics (fail safe — never delete because something was unreadable):
- PLATFORM_STORAGE_ACCOUNT unset -> get_platform_table_client() returns None;
  read_platform_clock() returns None. Tools have no clock of their own, so
  bound records are then never deleted (local dev / misconfiguration only).
- Clock row not found -> read_platform_clock() returns None.
- Table missing (TableNotFound) or any other error -> PlatformClockUnavailable;
  callers skip the record (timer) or refuse a new binding (503).
"""
import logging
import os
import re
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

PLATFORM_CLOCK_TABLE_DEFAULT = "retentionclock"
CLOCK_PARTITION = "COMPETITION"
_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class PlatformClockUnavailable(Exception):
    """The platform clock could not be read (network, RBAC, missing table, throttling)."""


def normalize_platform_id(value):
    """Lowercase, trimmed platform GUID, or None when it isn't one. Use it before every lookup and store."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if _GUID_RE.match(text) else None


def get_platform_table_client():
    """One client per request/sweep; pass it to read_platform_clock(table_client=...). None when unconfigured."""
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        return None
    table_name = os.environ.get("PLATFORM_CLOCK_TABLE") or PLATFORM_CLOCK_TABLE_DEFAULT
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
       or None when the id is not a GUID, the feature is off, or the row is absent.
    Raises PlatformClockUnavailable on any other failure.
    """
    guid = normalize_platform_id(platform_id)
    if guid is None:
        return None
    try:
        client = table_client or get_platform_table_client()
    except Exception as exc:  # credential / client construction
        raise PlatformClockUnavailable(type(exc).__name__) from exc
    if client is None:
        return None
    try:
        entity = client.get_entity(CLOCK_PARTITION, guid)
    except ResourceNotFoundError as exc:
        if getattr(exc, "error_code", None) == "TableNotFound" or "TableNotFound" in str(exc):
            logging.error("Platform clock table not found")
            raise PlatformClockUnavailable("TableNotFound") from exc
        return None
    except Exception as exc:
        logging.error("Platform clock read failed: %s", type(exc).__name__)
        raise PlatformClockUnavailable(type(exc).__name__) from exc
    return {
        "deletionDate": parse_utc(entity.get("DeletionDate")),
        "status": str(entity.get("Status") or "active"),
        "purged": bool(entity.get("PurgedUtc")),
    }


def effective_deletion(local_deletion, clock):
    """The platform date when the platform has one, else the tool's cached copy (aware UTC or None). Display only."""
    if clock and clock.get("deletionDate") is not None:
        return clock["deletionDate"]
    return parse_utc(local_deletion)


def platform_says_delete(clock):
    """True only on the platform's verdict: the competition is deleted or purged. The date alone never is."""
    if not clock:
        return False
    return bool(clock.get("purged")) or clock.get("status") == "deleted"


def to_iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
