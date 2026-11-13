from __future__ import annotations
import datetime

def get_local_timezone() -> datetime.tzinfo:
    """Retrieve the current local operating system timezone."""
    # datetime.now().astimezone() returns a timezone-aware object for the OS local zone
    tz = datetime.datetime.now().astimezone().tzinfo
    if tz is None:
        # Fallback in case of unexpected timezone configurations
        return datetime.timezone.utc
    return tz

def localize_datetime(dt: datetime.datetime) -> datetime.datetime:
    """Convert a datetime object to be timezone-aware in the local OS timezone."""
    local_tz = get_local_timezone()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def server_zone() -> datetime.tzinfo:
    """The server's timezone as a named zone, which follows summer-time changes.

    get_local_timezone() gives today's fixed offset: right for converting one timestamp now, but
    an hour-of-day profile over 28 days (services/traffic_usual) would put the weeks either side
    of a clock change an hour apart. TZ when set (systemd units and containers use it), else
    /etc/localtime, else that fixed offset.
    """
    import os
    from zoneinfo import ZoneInfo

    name = (os.environ.get("TZ") or "").lstrip(":").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # /etc/localtime is usually a link into the zone database: take the zone by its name
    # ("Africa/Lagos"), which the Traffic page shows, rather than as an anonymous "localtime".
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            key = target.split(marker, 1)[1]
            for prefix in ("posix/", "right/"):
                key = key[len(prefix):] if key.startswith(prefix) else key
            return ZoneInfo(key)
    except Exception:
        pass
    try:
        with open("/etc/localtime", "rb") as fh:
            return ZoneInfo.from_file(fh, key="localtime")
    except Exception:
        return get_local_timezone()
