# Chennai Bus Network Builder — project export

## What's in here

| File | What it is |
|---|---|
| `test.py` | The app itself — run this with Streamlit. Map-based route builder + Dispatch + Drivers + driver schedule, role-gated login. |
| `db.py` | All Postgres/PostGIS queries. |
| `auth.py` | Login. |
| `requirements.txt` | Everything to `pip install`. |
| `schema/001_dispatch_integration.sql` … `004_more_fleet.sql` | The migrations, in order, that built the schema on top of the base tables. |
| `schema/seed_app_users.py` | One-off script: creates a login for every driver row that doesn't have one yet. Safe to re-run. |
| `Hackatronicsdb_full_dump.sql` | **A full dump of the actual live database as of right now** — every real route, driver, bus, and dispatch that exists today, not just seed data. Restoring this gets you the exact current state. |

## Setting it up on a new machine

```bash
pip install -r requirements.txt

# 1. Create the database and restore everything into it in one shot —
#    this dump already includes the schema AND all the current data,
#    so you do NOT need to run the schema/*.sql migrations separately.
createdb Hackatronicsdb
psql -d Hackatronicsdb -f Hackatronicsdb_full_dump.sql

# 2. Point the app at your Postgres if it's not the default
#    (postgresql://postgres@localhost:5432/Hackatronicsdb):
export DATABASE_URL="postgresql://user:pass@host:5432/Hackatronicsdb"

# 3. Run it
streamlit run test.py
```

Demo logins already in the data (password `hackathon2026` for all): `dispatcher1`, `dispatcher2`, and `driver1` through `driver13`.

`DB_INTEGRATION_ENABLED = True` is already set at the top of `test.py`. PostGIS must be enabled on your Postgres — the dump includes `CREATE EXTENSION IF NOT EXISTS postgis`, so a normal Postgres install with the postgis extension package available is enough.

## What's NOT included, on purpose

An earlier version of this project had a separate `driverweb.py` prototype and a `tables.sql`/`Database` dump — both are now fully superseded (folded into `test.py`, `db.py`, and the `schema/` migrations respectively) and were left out so there's nothing stale to get confused by.
