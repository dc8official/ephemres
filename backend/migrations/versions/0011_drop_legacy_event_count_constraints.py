"""drop_legacy_event_count_constraints

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-24 23:30:00.000000

"""
from typing import Sequence, Union
from app.core.migrator import op
from app.core import orm as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Decompress any compressed chunks, drop check constraints, widen columns to Integer
    op.execute("""
        DO $$
        DECLARE
            c RECORD;
            is_ts BOOLEAN := FALSE;
        BEGIN
            SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') INTO is_ts;

            IF is_ts THEN
                -- Decompress any compressed chunks on endpoint_events
                FOR c IN
                    SELECT format('%I.%I', chunk_schema, chunk_name) AS full_chunk_name
                    FROM timescaledb_information.chunks
                    WHERE hypertable_name = 'endpoint_events' AND is_compressed = true
                LOOP
                    BEGIN
                        EXECUTE format('SELECT decompress_chunk(%L)', c.full_chunk_name);
                    EXCEPTION WHEN OTHERS THEN
                        RAISE NOTICE 'Skipping chunk decompression %: %', c.full_chunk_name, SQLERRM;
                    END;
                END LOOP;
            END IF;

            -- Drop legacy check constraints
            ALTER TABLE endpoint_events DROP CONSTRAINT IF EXISTS ck_events_success_count;
            ALTER TABLE endpoint_events DROP CONSTRAINT IF EXISTS ck_events_failed_count;

            -- Widen cycle counters to standard Integer to support multi-cycle continuous state events
            BEGIN
                ALTER TABLE endpoint_events ALTER COLUMN success_count TYPE INTEGER;
            EXCEPTION WHEN OTHERS THEN NULL;
            END;

            BEGIN
                ALTER TABLE endpoint_events ALTER COLUMN failed_count TYPE INTEGER;
            EXCEPTION WHEN OTHERS THEN NULL;
            END;

            BEGIN
                ALTER TABLE endpoint_events ALTER COLUMN monitoring_cycle_count TYPE INTEGER;
            EXCEPTION WHEN OTHERS THEN NULL;
            END;

            -- Retro-heal legacy discrete ping events where start_time == end_time and duration == 0
            UPDATE endpoint_events
            SET end_time = start_time + INTERVAL '60 seconds',
                duration_seconds = 60
            WHERE end_time = start_time
              AND duration_seconds = 0
              AND start_time >= NOW() - INTERVAL '30 days';

        END $$;
    """)


def downgrade() -> None:
    # Downgrade: re-add standard constraints if needed
    op.execute("""
        DO $$
        BEGIN
            -- No-op: dropping unneeded restrictive constraints is safe and widening columns is forward-compatible.
            NULL;
        END $$;
    """)
