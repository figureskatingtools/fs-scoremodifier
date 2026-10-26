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

Orphans (records the platform can no longer decide about). A live record is
orphaned when it has no PlatformId (or one that is not a GUID), or when a
successful read of its clock row found none. An orphan older than
ORPHAN_MIN_AGE_DAYS (by its created time; without one, by the newest timestamp
it has — record_age_reference) is deleted like a platform "deleted" verdict.
Before any of that, orphan_sweep_allowed() probes the clock table once per
sweep: unreadable, unconfigured, or holding zero rows while the tool has
bound records (wrong table/account) -> the orphan step is aborted for the run.
A read error on a single record is never "not found": that record is skipped.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

PLATFORM_CLOCK_TABLE_DEFAULT = "retentionclock"
CLOCK_PARTITION = "COMPETITION"
# An orphaned record (see the module docstring) younger than this is never touched.
ORPHAN_MIN_AGE_DAYS = 60
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


def clock_table_has_rows(table_client=None):
    """
    True when the clock table holds at least one competition row, False when
    a successful read found none, None when PLATFORM_STORAGE_ACCOUNT is unset.
    Raises PlatformClockUnavailable on any failure (missing table included).
    """
    try:
        client = table_client or get_platform_table_client()
    except Exception as exc:  # credential / client construction
        raise PlatformClockUnavailable(type(exc).__name__) from exc
    if client is None:
        return None
    try:
        rows = client.query_entities(
            f"PartitionKey eq '{CLOCK_PARTITION}'", select=["RowKey"], results_per_page=1)
        for _row in rows:
            return True
        return False
    except Exception as exc:
        logging.error("Platform clock probe failed: %s", type(exc).__name__)
        raise PlatformClockUnavailable(type(exc).__name__) from exc


def orphan_sweep_allowed(table_client, bound_record_count):
    """
    (allowed, reason) for this run's orphan step. Refused when the clock is
    unconfigured ("off") or unreadable ("unavailable"), or holds no rows at all
    while the tool has bound records ("empty" — a sign of a wrong table or
    account, under which every bound record would look orphaned).
    """
    try:
        has_rows = clock_table_has_rows(table_client)
    except PlatformClockUnavailable:
        return False, "unavailable"
    if has_rows is None:
        return False, "off"
    if not has_rows and bound_record_count > 0:
        return False, "empty"
    return True, "ok"


def record_age_reference(created, *other_timestamps):
    """
    The time an orphan's age counts from: its created time, or — when it has
    none that parses — the newest of its other timestamps. None when it has
    no usable timestamp at all (such a record is never treated as old).
    """
    reference = parse_utc(created)
    if reference is not None:
        return reference
    others = [dt for dt in (parse_utc(value) for value in other_timestamps) if dt is not None]
    return max(others) if others else None


def orphan_old_enough(reference, now, min_age_days=ORPHAN_MIN_AGE_DAYS):
    """True when `reference` (aware UTC) lies more than `min_age_days` before `now`."""
    return reference is not None and reference < now - timedelta(days=min_age_days)
