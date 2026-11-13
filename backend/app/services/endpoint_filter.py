"""The shared endpoint filter (v3.3.6 build plan, section 4.3): one definition, one place that
turns it into a set of endpoints.

A filter is site, location, tags, device type and search text. The rule, decided with Kenneth:
any value within one field, all fields together. Tags "mpls" and "core" match endpoints with
either tag; site "Abuja" plus tag "mpls" matches only Abuja endpoints tagged "mpls". Every field
is optional, and an empty filter matches every endpoint that isn't deleted.

The endpoint list, the dashboard counts (through /endpoints/match), reports, CSV, PDF and the
topology map all go through `conditions()` or `matching_ids()`. No route keeps its own copy of
the matching rules, so the same filter gives the same endpoints everywhere.

The dashboard's status tiles (up, unstable, down, no status) are not part of it: status is the
dashboard's view of the endpoints it shows, and means nothing to a report or the map.

Site and location match case-insensitively; tags are stored lower case. Values are kept as first
typed (normalise_label only trims and collapses spaces). An endpoint with no site, location or
tags works as before: it just never matches a filter on them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from app.core.orm import AsyncSession, BinaryExpression, func, or_, select

from app.models.endpoint import Endpoint

MAX_LABEL_LENGTH = 100
MAX_TAGS_PER_ENDPOINT = 20
MAX_VALUES_PER_FIELD = 50
MAX_SEARCH_LENGTH = 200

# Tags travel as a PostgreSQL array and through backups as its text form ({a,b}), which
# core/restore.py splits on commas. These characters would break that, and are poor tags anyway.
_FORBIDDEN_TAG_CHARS = re.compile(r'[,{}"\\]')
_SPACES = re.compile(r"\s+")


def normalise_label(value: Any) -> Optional[str]:
    """A site or location as stored: trimmed, inner whitespace collapsed, case kept. Empty is None."""
    if value is None:
        return None
    text = _SPACES.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:MAX_LABEL_LENGTH]


def normalise_tag(value: Any) -> Optional[str]:
    """A tag as stored: like a label, but lower case. Raises ValueError on characters a tag
    can't hold (see _FORBIDDEN_TAG_CHARS)."""
    text = normalise_label(value)
    if text is None:
        return None
    if _FORBIDDEN_TAG_CHARS.search(text):
        raise ValueError(f"Tag '{text}' can't contain commas, quotes, backslashes or braces.")
    return text.lower()


def normalise_tags(values: Optional[Iterable[Any]]) -> List[str]:
    """Tags for one endpoint: normalised, empty ones dropped, duplicates removed in order."""
    seen: List[str] = []
    for raw in values or []:
        tag = normalise_tag(raw)
        if tag and tag not in seen:
            seen.append(tag)
    if len(seen) > MAX_TAGS_PER_ENDPOINT:
        raise ValueError(f"An endpoint can have at most {MAX_TAGS_PER_ENDPOINT} tags.")
    return seen


def _distinct_labels(values: Optional[Iterable[Any]], tag: bool = False) -> Tuple[str, ...]:
    """Filter values for one field: normalised, deduplicated case-insensitively, bounded."""
    out: List[str] = []
    lowered: set = set()
    for raw in values or []:
        value = normalise_tag(raw) if tag else normalise_label(raw)
        if value and value.lower() not in lowered:
            lowered.add(value.lower())
            out.append(value)
    return tuple(out[:MAX_VALUES_PER_FIELD])


@dataclass(frozen=True)
class EndpointFilter:
    sites: Tuple[str, ...] = ()
    locations: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    device_types: Tuple[str, ...] = ()
    q: str = ""

    @classmethod
    def build(
        cls,
        site: Optional[Sequence[Any]] = None,
        location: Optional[Sequence[Any]] = None,
        tag: Optional[Sequence[Any]] = None,
        type: Optional[Sequence[Any]] = None,
        q: Optional[str] = None,
    ) -> "EndpointFilter":
        """From request values (query parameters or a JSON body). Single strings are accepted
        where a list is expected. Raises ValueError for a tag that can't exist."""
        def as_list(v: Any) -> List[Any]:
            if v is None:
                return []
            if isinstance(v, (str, bytes)):
                return [v]
            return list(v)

        search = _SPACES.sub(" ", str(q or "")).strip()[:MAX_SEARCH_LENGTH]
        return cls(
            sites=_distinct_labels(as_list(site)),
            locations=_distinct_labels(as_list(location)),
            tags=_distinct_labels(as_list(tag), tag=True),
            device_types=_distinct_labels(as_list(type)),
            q=search,
        )

    def is_empty(self) -> bool:
        return not (self.sites or self.locations or self.tags or self.device_types or self.q)

    def as_dict(self) -> dict:
        """The filter as the API returns it, and as the browser sends it back."""
        return {
            "site": list(self.sites),
            "location": list(self.locations),
            "tag": list(self.tags),
            "type": list(self.device_types),
            "q": self.q,
        }

    def describe(self) -> str:
        """One line for people, e.g. the PDF: 'Site Abuja · Tags mpls, core · Search "gw"'."""
        parts = []
        if self.sites:
            parts.append(("Sites " if len(self.sites) > 1 else "Site ") + ", ".join(self.sites))
        if self.locations:
            parts.append(("Locations " if len(self.locations) > 1 else "Location ") + ", ".join(self.locations))
        if self.tags:
            parts.append(("Tags " if len(self.tags) > 1 else "Tag ") + ", ".join(self.tags))
        if self.device_types:
            parts.append(("Types " if len(self.device_types) > 1 else "Type ") + ", ".join(self.device_types))
        if self.q:
            parts.append(f'Search "{self.q}"')
        return " · ".join(parts) if parts else "All endpoints"


def or_empty(value: Any) -> EndpointFilter:
    """`value` if it is a filter, else the empty filter. Routes take the filter through a
    dependency; code that calls a route function directly (tests, other routes) gets the
    dependency marker instead, which must read as "no filter"."""
    return value if isinstance(value, EndpointFilter) else EndpointFilter()


def _like_pattern(text: str) -> str:
    """ILIKE pattern for 'contains', with the user's % and _ taken literally."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def conditions(f: EndpointFilter) -> List[Any]:
    """WHERE conditions on Endpoint for this filter, deleted endpoints always excluded.

    Within a field: any value (IN, or array overlap for tags). Across fields: all (the caller
    ANDs the list, as select().where(*conditions) does)."""
    conds: List[Any] = [Endpoint.endpoint_status != "DELETED"]
    if f.sites:
        conds.append(func.lower(Endpoint.site).in_([s.lower() for s in f.sites]))
    if f.locations:
        conds.append(func.lower(Endpoint.location).in_([l.lower() for l in f.locations]))
    if f.tags:
        # tags && ARRAY[...]: shares at least one tag. Served by the GIN index (migration 0023).
        conds.append(BinaryExpression(Endpoint.tags, "&&", list(f.tags)))
    if f.device_types:
        conds.append(func.lower(Endpoint.device_type).in_([t.lower() for t in f.device_types]))
    if f.q:
        # Search is the one "contains" field: name, address, site, location, description, tags.
        pattern = _like_pattern(f.q)
        conds.append(or_(
            Endpoint.hostname.ilike(pattern),
            func.host(Endpoint.ip_address).ilike(pattern),
            Endpoint.site.ilike(pattern),
            Endpoint.location.ilike(pattern),
            Endpoint.description.ilike(pattern),
            func.array_to_string(Endpoint.tags, " ").ilike(pattern),
        ))
    return conds


async def matching_ids(db: AsyncSession, f: EndpointFilter) -> List[UUID]:
    """Ids of the endpoints the filter matches, ordered by hostname."""
    res = await db.execute(
        select(Endpoint.id).where(*conditions(f)).order_by(Endpoint.hostname.asc())
    )
    return list(res.scalars().all())


async def resolve_scope(
    db: AsyncSession,
    f: Optional[EndpointFilter],
    endpoint_ids: Optional[Sequence[UUID]],
) -> List[UUID]:
    """The endpoints a report or export covers: a filter, a hand-picked list, or both (the
    picked endpoints that the filter matches, in the order they were picked). Neither gives an
    empty list; the caller decides whether that is an error."""
    picked = list(endpoint_ids or [])
    if f is None or f.is_empty():
        return picked
    matched = await matching_ids(db, f)
    if not picked:
        return matched
    allowed = {str(i) for i in matched}
    return [i for i in picked if str(i) in allowed]


async def traffic_scope(
    db: AsyncSession,
    f: Optional[EndpointFilter],
    device_id: Optional[UUID] = None,
) -> Optional[List[UUID]]:
    """The routers whose traffic a Traffic page view covers (build plan 7.5 step 2), or None for
    "every router, and flows from sources not yet registered".

    The filter picks routers: their traffic is what they saw, and hosts are not filtered on
    their own. The flow and SNMP tables key every row by the endpoint that reported it, so the
    endpoints the filter matches are used as they are: only routers have rows, and a router
    that exports from its own address without being marked as an exporter is still found. It is
    the list `matching_ids` gives the dashboard, reports and the map, so the same filter covers
    the same routers on every page.

    `device_id` narrows to one device: the filter's routers if it is among them, else nothing
    (a device the filter excludes shows no traffic rather than ignoring the filter), the way
    `resolve_scope` treats a hand-picked list.
    """
    f = or_empty(f)
    if f.is_empty():
        return None if device_id is None else [device_id]
    matched = await matching_ids(db, f)
    if device_id is None:
        return matched
    return [device_id] if str(device_id) in {str(i) for i in matched} else []


FILTER_QUERY_KEYS = ("site", "location", "tag", "type", "q")


def filter_from_request(request: Any) -> EndpointFilter:
    """A route dependency: the filter from the query string, the same keys the browser keeps in
    the page URL (?site=Abuja&tag=mpls&tag=core&q=gw). Repeated keys give several values. A tag
    that can't exist answers 400."""
    from app.core.asgi import HTTPException

    params = request.query_params
    try:
        return EndpointFilter.build(
            site=params.getlist("site"),
            location=params.getlist("location"),
            tag=params.getlist("tag"),
            type=params.getlist("type"),
            q=params.get("q"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def filter_from_body(body: Optional[dict]) -> Optional[EndpointFilter]:
    """The filter from a JSON body field ({"site": [...], "tag": [...], "q": "..."}), or None.
    A tag that can't exist answers 400."""
    if not body:
        return None
    from app.core.asgi import HTTPException

    try:
        return EndpointFilter.build(
            site=body.get("site"),
            location=body.get("location"),
            tag=body.get("tag"),
            type=body.get("type"),
            q=body.get("q"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
