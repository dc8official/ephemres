"""Site and tags on endpoints, for the shared filter

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-11 23:30:00.000000

v3.3.6 lets every page filter endpoints by site, location and tags (services/endpoint_filter.py).
They are free text on the endpoint, "tags with hierarchy" in Kenneth's words: no managed site
table, no timezone, everything optional.

- endpoints.site: one per endpoint, e.g. "Abuja". Starts empty on upgrade.
- endpoints.location: unchanged. Its existing free text is kept and now reads as the location
  within the site; an admin can put it under a site with bulk assign.
- endpoints.tags: any number, lower case (the API normalises them), e.g. {"mpls router"}.

Matching is case-insensitive, so the site and location indexes are on lower(); the GIN index
serves "has any of these tags" (&&).
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS site VARCHAR(255)")
    op.execute("ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_endpoints_site_lower ON endpoints (lower(site))")
    op.execute("CREATE INDEX IF NOT EXISTS ix_endpoints_location_lower ON endpoints (lower(location))")
    op.execute("CREATE INDEX IF NOT EXISTS ix_endpoints_tags ON endpoints USING GIN (tags)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_endpoints_tags")
    op.execute("DROP INDEX IF EXISTS ix_endpoints_location_lower")
    op.execute("DROP INDEX IF EXISTS ix_endpoints_site_lower")
    op.execute("ALTER TABLE endpoints DROP COLUMN IF EXISTS tags")
    op.execute("ALTER TABLE endpoints DROP COLUMN IF EXISTS site")
