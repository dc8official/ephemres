# TimescaleDB integration tests

Tests in this directory are marked `timescale`. They run the real migrations against a
throwaway TimescaleDB and call the API routers with real SQL, so they catch what the
mock-based unit tests cannot: query semantics, continuous-aggregate behaviour and the exact
numbers the dashboards show.

Without `EPHEMRES_TEST_TIMESCALE_DSN` they are skipped, so the default suite is unchanged.

```bash
eval "$(tests/integration/timescale.sh start)"
.venv/bin/python -m pytest -m timescale
tests/integration/timescale.sh stop
```

`EPHEMRES_TEST_TIMESCALE_DSN` must point at a server where the user can create databases
(the default container uses the `postgres` superuser). Each test session creates its own
`ephemres_it_<random>` database, migrates it to head and drops it afterwards. Never point it at
a database you care about.

## Characterization tests

`test_flow_dashboard_characterization.py` pins what the flow endpoints return today for a
small hand-computed dataset, including behaviour the v3.4 plan
(`docs/architecture/v3.4-dashboard-data-and-design-plan.md`) intends to change, such as the
50/50 split of transit traffic and the materialized-only lag of the hourly aggregate. When a
phase changes one of those behaviours, update the matching assertion in the same commit and
say why.
