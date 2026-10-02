from datetime import datetime, timedelta, timezone

# America/Sao_Paulo is the project's display timezone. Timestamps are still
# stored as UTC ISO strings (db.now()) - retention/stale-session queries
# compare those strings directly - and only converted here for display.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    # No tzdata available (e.g. a Windows dev box without the tzdata
    # package). Brazil has had no DST since 2019, so a fixed -03:00 matches.
    LOCAL_TZ = timezone(timedelta(hours=-3), "BRT")


def to_local(value):
    """Parses a stored ISO timestamp and returns it in São Paulo time, or
    None when it's empty/unparseable. Naive values are treated as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ)


def format_local(value, fmt="%d-%m-%Y %H:%M"):
    local = to_local(value)
    if local is None:
        return value or ""
    return local.strftime(fmt)
