"""
PostGIS persistence layer for the Chennai Bus Network Builder.

This targets the EXISTING schema from the team's `Hackatronicsdb` dump
(the `Database` file next to this one) exactly, rather than inventing a
parallel one. Extracted straight from that dump's own CREATE TABLE
statements:

    bus_routes(route_id PK serial, route_name varchar(100), route_code
               varchar(10) UNIQUE, path geography(LineString,4326),
               created_at)
    bus_stops(stop_id PK bigint -- NO sequence/default: populated from real
              OSM node IDs, which are always positive -- stop_name,
              stop_code, operator, shelter, wheelchair, source,
              geom geometry(Point,4326))
    route_stops(route_id, stop_id FK->bus_stops ON DELETE CASCADE,
                stop_sequence, direction smallint default 0,
                PK(route_id, stop_id, direction, stop_sequence))
                -- NOTE: no FK route_id->bus_routes in their schema, so
                -- route deletion here must clean up route_stops manually.

Import-safe with no Postgres running at all: every public function
no-ops (returns None/False) if a connection can't be established, so the
Streamlit app never breaks or hangs waiting on a database that isn't there.

Toggle DB_INTEGRATION_ENABLED in test.py to turn this on once real
Postgres + PostGIS is reachable — nothing else needs to change.

>>> WHAT WAS ACTUALLY VERIFIED (see NOTES.md for the full writeup) <<<
No PostGIS instance was available to test against in this environment
(no docker/brew/root, and the pip-installable embedded Postgres used to
smoke-test the connection/transaction logic doesn't bundle PostGIS). So:
  - Verified for real, against a live throwaway Postgres 16 server:
    connection handling, cursor lifecycle, INSERT...RETURNING, the
    negative-ID sequence trick, the find-or-create relational logic, FK
    ordering on delete, and transaction commit/rollback on error.
  - Written correctly per the standard PostGIS API (ST_MakePoint,
    ST_GeomFromText, ST_DWithin, ::geography casts) but NOT independently
    executed against real PostGIS — that part you'll want to sanity-check
    the first time this runs against your actual database.
"""

import os
import re
from datetime import timedelta

import streamlit as st

try:
    import psycopg2

    _PSYCOPG2_AVAILABLE = True
except ImportError:
    # No Postgres driver installed at all (expected in this dev environment
    # — there's no Postgres to talk to). Every function below degrades to a
    # no-op rather than crashing the app on `import db`.
    _PSYCOPG2_AVAILABLE = False

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres@localhost:5432/Hackatronicsdb",
)

CONNECT_TIMEOUT_S = 3  # fail fast instead of hanging the app when there's no DB
STOP_MATCH_TOLERANCE_M = 100  # reuse an existing bus_stops row within this radius


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _get_connection():
    """One cached connection per session. Returns None (never raises) if
    Postgres isn't reachable, so every caller below can just check for None
    instead of wrapping every call site in its own try/except."""
    if not _PSYCOPG2_AVAILABLE:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT_S)
        conn.autocommit = False
        return conn
    except Exception:
        return None


def db_available():
    return _get_connection() is not None


def ensure_extras():
    """Idempotent, additive-only setup: a sequence for synthetic stop IDs.
    Does NOT touch the team's existing tables/columns/rows.

    NOTE on every function below calling conn.commit() even after a plain
    SELECT: with autocommit=False, a read-only query still opens a
    transaction that stays open (and holds locks) until it's explicitly
    closed. A cached connection (_get_connection is @st.cache_resource)
    left "idle in transaction" between reruns can and did block a later
    ALTER TABLE from a completely different session — hit this for real
    while testing, not theoretical."""
    conn = _get_connection()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SEQUENCE IF NOT EXISTS custom_stop_id_seq;")
        conn.commit()
    except Exception:
        conn.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_route_code(route_name, cur):
    """route_code is VARCHAR(10) UNIQUE. Derive it from the name, then
    disambiguate with a numeric suffix if that collides with an existing
    route_code."""
    base = re.sub(r"[^A-Za-z0-9]", "", route_name).upper()[:8] or "ROUTE"
    for suffix in [""] + [str(i) for i in range(1, 100)]:
        candidate = (base + suffix)[:10]
        cur.execute("SELECT 1 FROM bus_routes WHERE route_code = %s", (candidate,))
        if cur.fetchone() is None:
            return candidate
    raise RuntimeError("Could not generate a unique route_code")


def _linestring_wkt(geometry_latlon):
    """geometry_latlon: list of [lat, lon] (how test.py stores OSRM's
    geometry). WKT/PostGIS coordinate order is lon, then lat."""
    pts = ", ".join(f"{lon} {lat}" for lat, lon in geometry_latlon)
    return f"LINESTRING({pts})"


def _parse_linestring_wkt(wkt):
    """Inverse of _linestring_wkt: 'LINESTRING(lon lat, lon lat, ...)' ->
    [[lat, lon], ...] — used to reconstruct a route's geometry when loading
    it back from Postgres (ST_AsText output uses the same lon-lat order)."""
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    points = []
    for pair in inner.split(","):
        lon_str, lat_str = pair.strip().split(" ")
        points.append([float(lat_str), float(lon_str)])
    return points


def _find_or_create_stop(cur, lat, lon, stop_name):
    """Reuse an existing bus_stops row within STOP_MATCH_TOLERANCE_M — real
    stops seeded from OSM already live in this table — or insert a new one.

    Manually-placed stops get a NEGATIVE stop_id (via custom_stop_id_seq).
    bus_stops.stop_id has no default/sequence in the team's schema — it's
    populated from real OSM node IDs, which are always positive — so
    negative IDs can never collide with genuine OSM-sourced stops.
    """
    cur.execute(
        """
        SELECT stop_id FROM bus_stops
        WHERE ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
            %s
        )
        ORDER BY geom::geography <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
        LIMIT 1
        """,
        (lon, lat, STOP_MATCH_TOLERANCE_M, lon, lat),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]

    # CREATE SEQUENCE IF NOT EXISTS here too (not just in ensure_extras()) —
    # verified against real PostGIS that skipping ensure_extras() (e.g. a
    # caller that saves a route without it ever having run) makes this
    # nextval() fail with "relation custom_stop_id_seq does not exist" and
    # silently kills the whole save. Idempotent and cheap, so just always
    # do it rather than depend on call-order from the app layer.
    cur.execute("CREATE SEQUENCE IF NOT EXISTS custom_stop_id_seq")
    cur.execute("SELECT -nextval('custom_stop_id_seq')")
    new_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO bus_stops (stop_id, stop_name, source, geom)
        VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
        """,
        (new_id, stop_name or "Unnamed stop", "streamlit-builder", lon, lat),
    )
    return new_id


# ---------------------------------------------------------------------------
# Public API — called from test.py
# ---------------------------------------------------------------------------
def save_route_to_db(route_name, route, locality_lookup=None, duration_min=None):
    """Persist a saved route (the dict shape test.py builds for
    st.session_state.routes[route_name]) into bus_routes / bus_stops /
    route_stops. Returns the new route_id, or None if the DB isn't
    reachable or the save failed — callers should treat that as non-fatal,
    the route still lives in st.session_state either way.

    `locality_lookup(lat, lon) -> str | None` is optional; when given it's
    used as stop_name for newly created bus_stops rows (test.py passes its
    reverse-geocoded locality name here).

    `duration_min` is the route's one-way OSRM travel-time estimate, in
    minutes — persisted so the Dispatch tab's cycle-count math has a real
    number to use later, since it can't be derived from geometry alone.
    """
    conn = _get_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            route_code = _make_route_code(route_name, cur)
            wkt = _linestring_wkt(route["geometry"])
            cur.execute(
                """
                INSERT INTO bus_routes (route_name, route_code, path, avg_duration_min)
                VALUES (%s, %s, ST_GeomFromText(%s, 4326)::geography, %s)
                RETURNING route_id
                """,
                (route_name, route_code, wkt, duration_min),
            )
            route_id = cur.fetchone()[0]

            for seq, (lat, lon) in enumerate(route["stops"], start=1):
                name = locality_lookup(lat, lon) if locality_lookup else None
                stop_id = _find_or_create_stop(cur, lat, lon, name)
                cur.execute(
                    """
                    INSERT INTO route_stops (route_id, stop_id, stop_sequence, direction)
                    VALUES (%s, %s, %s, 0)
                    """,
                    (route_id, stop_id, seq),
                )
        conn.commit()
        return route_id
    except Exception:
        conn.rollback()
        return None


def delete_route_from_db(route_id):
    """Remove a route and its stop links. bus_stops rows are left in place
    (other routes may reference them) — matches the team's schema, which
    has no ON DELETE CASCADE from bus_routes to route_stops."""
    conn = _get_connection()
    if conn is None or route_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM route_stops WHERE route_id = %s", (route_id,))
            cur.execute("DELETE FROM bus_routes WHERE route_id = %s", (route_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


# A route saved without an explicit color (i.e. reconstructed from the DB,
# which has no color column) gets one from here, cycling by position —
# purely cosmetic, doesn't need to be persisted.
_ROUTE_COLOR_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def load_all_routes_from_db():
    """Reconstructs the route dicts test.py keeps in st.session_state.routes,
    straight from Postgres — so routes saved in a PREVIOUS session (or
    seeded directly via SQL, like schema/002_seed_chennai_demo_data.sql)
    actually show up instead of only ever existing in memory. Returns
    {route_name: route_dict}, same shape save_route_to_db is given.

    Includes a real "stop_names" list (from bus_stops.stop_name) alongside
    the usual (lat, lon) "stops" — used by the View Routes tab's
    from/to place search, which needs actual names, not just coordinates.
    """
    conn = _get_connection()
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT route_id, route_name, route_code, ST_AsText(path),
                       avg_duration_min, ST_Length(path) / 1000.0
                FROM bus_routes
                ORDER BY route_id
                """
            )
            route_rows = cur.fetchall()

            cur.execute(
                """
                SELECT rs.route_id, rs.stop_sequence, s.stop_name, ST_Y(s.geom), ST_X(s.geom)
                FROM route_stops rs
                JOIN bus_stops s ON s.stop_id = rs.stop_id
                ORDER BY rs.route_id, rs.stop_sequence
                """
            )
            stop_rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return {}

    stops_by_route, names_by_route = {}, {}
    for route_id, seq, name, lat, lon in stop_rows:
        stops_by_route.setdefault(route_id, []).append((lat, lon))
        names_by_route.setdefault(route_id, []).append(name)

    routes = {}
    for i, (route_id, route_name, route_code, wkt, avg_duration_min, length_km) in enumerate(route_rows):
        length_km = round(length_km, 2) if length_km else 0.0
        routes[route_name] = {
            "color": _ROUTE_COLOR_PALETTE[i % len(_ROUTE_COLOR_PALETTE)],
            "stops": stops_by_route.get(route_id, []),
            "stop_names": names_by_route.get(route_id, []),
            "geometry": _parse_linestring_wkt(wkt) if wkt else [],
            "distance_km": length_km,
            "duration_min": _duration_or_estimate(avg_duration_min, length_km),
            "optimized": False,
            "route_code": route_code,
            "db_route_id": route_id,
        }
    return routes


def list_all_buses():
    """Every bus, with its current route + driver — used by the "assign a
    bus to this route" picker on Save Route, and by the View Routes tab's
    edit-assignment controls. Deliberately simple (no time-window/conflict
    check, unlike list_available_buses): this just sets a bus's normal/
    default route (buses.route_id), not a scheduled shift (that's still
    what the Dispatch tab's create_duty is for)."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bus_id, bus_number, model, status, route_id, current_driver_id "
                "FROM buses ORDER BY bus_number"
            )
            rows = cur.fetchall()
        conn.commit()  # close the transaction even for a read — see note on ensure_extras()
    except Exception:
        conn.rollback()
        return []
    return [
        {"bus_id": bid, "bus_number": num, "model": model, "status": status,
         "route_id": rid, "current_driver_id": did}
        for bid, num, model, status, rid, did in rows
    ]


def list_drivers():
    """Simple driver list (id + name only) for pickers that don't need
    availability/hours context (unlike list_driver_availability)."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT driver_id, full_name FROM bus_drivers ORDER BY full_name")
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [{"driver_id": did, "full_name": name} for did, name in rows]


def assign_bus_to_route(bus_id, route_id):
    """Sets a bus's default route (buses.route_id). Separate from the
    Dispatch tab's create_duty: this has no driver/time involved, so it
    does NOT touch current_driver_id and does not fire fn_log_driver_swap."""
    conn = _get_connection()
    if conn is None or bus_id is None or route_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE buses SET route_id = %s WHERE bus_id = %s", (route_id, bus_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def clear_bus_route(bus_id):
    """Unassigns a bus from whatever route it's on (route_id -> NULL)."""
    conn = _get_connection()
    if conn is None or bus_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE buses SET route_id = NULL WHERE bus_id = %s", (bus_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def assign_driver_to_bus(bus_id, driver_id):
    """Sets (or clears, if driver_id is None) a bus's current_driver_id.
    This DOES fire fn_log_driver_swap() for real — same as create_duty —
    writing a driver_assignments_history row."""
    conn = _get_connection()
    if conn is None or bus_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE buses SET current_driver_id = %s WHERE bus_id = %s", (driver_id, bus_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def remove_driver_shift(driver_id):
    """Ends a driver's current shift: clears current_driver_id on whatever
    bus(es) they're on (fires fn_log_driver_swap for real, same as
    assign_driver_to_bus) and marks their in-progress duty_schedules row(s)
    CANCELLED so the record doesn't linger showing a stale SCHEDULED/
    OVERTIME_REQUIRED status for a shift that's been pulled."""
    conn = _get_connection()
    if conn is None or driver_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bus_id FROM buses WHERE current_driver_id = %s", (driver_id,))
            bus_ids = [row[0] for row in cur.fetchall()]
            for bus_id in bus_ids:
                cur.execute("UPDATE buses SET current_driver_id = NULL WHERE bus_id = %s", (bus_id,))
            cur.execute(
                "UPDATE duty_schedules SET status = 'CANCELLED' "
                "WHERE driver_id = %s AND status NOT IN ('CANCELLED', 'COMPLETED')",
                (driver_id,),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False


def list_drivers_overview():
    """Every driver with their currently-assigned bus/route (if any) — for
    the dispatcher-facing Drivers tab roster. A driver's full duty history
    (for "their schedule" / "amount of time they work") is fetched
    separately per-driver via get_driver_schedule, to keep this one light."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.driver_id, d.full_name, d.license_number, d.phone, d.shift_status,
                       d.shift_start_time, d.shift_end_time, d.min_rest_hours, d.max_daily_hours,
                       b.bus_number, r.route_name
                FROM bus_drivers d
                LEFT JOIN buses b ON b.current_driver_id = d.driver_id
                LEFT JOIN bus_routes r ON r.route_id = b.route_id
                ORDER BY d.full_name
                """
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [
        {
            "driver_id": did, "full_name": name, "license_number": lic, "phone": phone,
            "shift_status": status, "shift_start_time": sst, "shift_end_time": se,
            "min_rest_hours": float(minr), "max_daily_hours": float(maxd),
            "bus_number": bus_num, "route_name": route_name,
        }
        for did, name, lic, phone, status, sst, se, minr, maxd, bus_num, route_name in rows
    ]


# ---------------------------------------------------------------------------
# Dispatch / driver schedule — schema/001_dispatch_integration.sql
#
# Ported from driverweb.py's prototype, rewritten against the REAL tables
# (bus_routes/buses/bus_drivers) instead of its own throwaway routes/
# buses/crew tables, and against the reconciled duty_status enum (which
# uses OVERTIME_REQUIRED, not driverweb.py's original SCHEDULED_OVERTIME).
# ---------------------------------------------------------------------------
# Routes seeded/saved without a real OSRM travel-time estimate (avg_duration_min
# is NULL) fall back to this assumed average speed to still produce a usable
# duration for the cycle-count math — a rough Chennai city-bus assumption,
# not a measurement.
ASSUMED_CITY_BUS_SPEED_KMH = 20.0


def _duration_or_estimate(avg_duration_min, length_km):
    if avg_duration_min is not None:
        return float(avg_duration_min)
    if not length_km:
        return 0.0
    return round(length_km / ASSUMED_CITY_BUS_SPEED_KMH * 60.0, 1)


def list_dispatchable_routes():
    """Every saved route, with its real road length derived on the fly from
    the stored geometry (ST_Length on a geography column returns meters) —
    no redundant length column needed — plus how many stops it has, and a
    one-way duration estimate (real, from OSRM at save time, if available —
    else a distance/assumed-speed fallback) used for the Dispatch tab's
    cycle-count math."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.route_id, r.route_name, r.route_code,
                       ST_Length(r.path) / 1000.0 AS length_km,
                       r.avg_duration_min,
                       COUNT(rs.stop_id) AS stop_count
                FROM bus_routes r
                LEFT JOIN route_stops rs ON rs.route_id = r.route_id
                GROUP BY r.route_id, r.route_name, r.route_code, r.path, r.avg_duration_min
                ORDER BY r.route_name
                """
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    result = []
    for rid, name, code, km, avg_duration_min, n in rows:
        length_km = round(km, 2) if km else 0.0
        result.append({
            "route_id": rid, "route_name": name, "route_code": code,
            "length_km": length_km, "stop_count": n,
            "duration_min": _duration_or_estimate(avg_duration_min, length_km),
        })
    return result


def list_route_stops(route_id):
    """Ordered stops for one route — lets the dispatcher see what a route
    actually covers before assigning a shift to it (driverweb.py never
    showed stops at all, only a route name)."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rs.stop_sequence, s.stop_id, s.stop_name,
                       ST_Y(s.geom) AS lat, ST_X(s.geom) AS lon
                FROM route_stops rs
                JOIN bus_stops s ON s.stop_id = rs.stop_id
                WHERE rs.route_id = %s
                ORDER BY rs.stop_sequence
                """,
                (route_id,),
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [
        {"stop_sequence": seq, "stop_id": sid, "stop_name": name, "lat": lat, "lon": lon}
        for seq, sid, name, lat, lon in rows
    ]


MIN_SHIFT_GAP_MINUTES = 30  # required buffer between one shift ending and the next starting


def _naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _shift_conflicts(existing_shifts, new_start, new_end, min_gap_minutes=MIN_SHIFT_GAP_MINUTES):
    """True if (new_start, new_end) overlaps any of existing_shifts, or comes
    within min_gap_minutes of one on either side."""
    gap = timedelta(minutes=min_gap_minutes)
    new_start, new_end = _naive(new_start), _naive(new_end)
    for es, ee in existing_shifts:
        es, ee = _naive(es), _naive(ee)
        if new_start < ee and es < new_end:
            return True  # direct overlap
        if new_start >= ee and (new_start - ee) < gap:
            return True  # new shift starts too soon after this one ends
        if es >= new_end and (es - new_end) < gap:
            return True  # this one starts too soon after the new shift ends
    return False


def list_driver_availability(target_date, shift_hours=0.0, new_start=None, new_end=None):
    """Every driver, with hours already worked on target_date, whether
    adding a shift of `shift_hours` more today would push them over
    max_daily_hours, and — when new_start/new_end are given — whether that
    exact proposed shift window conflicts with (overlaps, or comes within
    MIN_SHIFT_GAP_MINUTES of) any of their existing non-cancelled shifts.

    The capacity check used to be a time-since-last-shift check against
    min_rest_hours instead — replaced because that criterion gave confusing
    results in practice (e.g. flagging a driver as a "rest violation" while
    sitting at a perfectly fine 7.5/8 hours worked, just because their last
    shift happened to end too recently before the new one's default start
    time). What actually matters for capacity is worked_today + shift_hours
    vs. max_daily_hours. The schedule-conflict check here is a separate,
    genuine scheduling constraint: two shifts overlapping (or too close
    together) for the same driver is a real impossibility, not a soft
    business-rule warning like capacity is.
    """
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.driver_id, d.full_name, d.min_rest_hours, d.max_daily_hours,
                       COALESCE(SUM(ds.working_hours) FILTER (
                           WHERE ds.start_time::date = %s AND ds.status != 'CANCELLED'
                       ), 0) AS worked_today
                FROM bus_drivers d
                LEFT JOIN duty_schedules ds ON ds.driver_id = d.driver_id
                GROUP BY d.driver_id, d.full_name, d.min_rest_hours, d.max_daily_hours
                ORDER BY d.full_name
                """,
                (target_date,),
            )
            rows = cur.fetchall()

            shifts_by_driver = {}
            if new_start is not None and new_end is not None:
                cur.execute(
                    "SELECT driver_id, start_time, end_time FROM duty_schedules "
                    "WHERE status != 'CANCELLED' AND driver_id IS NOT NULL"
                )
                for did, es, ee in cur.fetchall():
                    shifts_by_driver.setdefault(did, []).append((es, ee))
        conn.commit()
    except Exception:
        conn.rollback()
        return []

    result = []
    for driver_id, full_name, min_rest, max_daily, worked_today in rows:
        min_rest, max_daily, worked_today = float(min_rest), float(max_daily), float(worked_today)
        schedule_ok = True
        if new_start is not None and new_end is not None:
            schedule_ok = not _shift_conflicts(shifts_by_driver.get(driver_id, []), new_start, new_end)
        result.append({
            "driver_id": driver_id,
            "full_name": full_name,
            "min_rest_hours": min_rest,
            "max_daily_hours": max_daily,
            "worked_today": worked_today,
            "capacity_ok": (worked_today + shift_hours) <= max_daily,
            "schedule_ok": schedule_ok,
        })
    return result


def route_schedule_conflict(route_id, new_start, new_end, min_gap_minutes=MIN_SHIFT_GAP_MINUTES):
    """True if this ROUTE already has another non-cancelled duty (on any
    other bus/driver) whose time overlaps, or comes within
    min_gap_minutes of, the proposed window.

    This is a separate check from list_driver_availability's schedule_ok:
    that one only stops the SAME driver being double-booked across two of
    their own duties. It never caught two DIFFERENT drivers/buses both
    being dispatched onto the same route at overlapping times (found this
    for real: driver 1 and driver 2 ended up on the same route within the
    same window) — this closes that specific hole.
    """
    conn = _get_connection()
    if conn is None or route_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT start_time, end_time FROM duty_schedules WHERE route_id = %s AND status != 'CANCELLED'",
                (route_id,),
            )
            existing = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return False
    return _shift_conflicts(existing, new_start, new_end, min_gap_minutes)


def list_available_buses(start_dt, end_dt):
    """Buses that are operationally ACTIVE and not already booked on an
    overlapping duty. driverweb.py only checked driver rest hours — it
    never checked this, so the same bus could be double-booked."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bus_id, b.bus_number, b.model, b.capacity
                FROM buses b
                WHERE b.status = 'ACTIVE'
                  AND NOT EXISTS (
                      SELECT 1 FROM duty_schedules ds
                      WHERE ds.bus_id = b.bus_id
                        AND ds.status != 'CANCELLED'
                        AND (ds.start_time, ds.end_time) OVERLAPS (%s, %s)
                  )
                ORDER BY b.bus_number
                """,
                (start_dt, end_dt),
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [{"bus_id": bid, "bus_number": num, "model": model, "capacity": cap} for bid, num, model, cap in rows]


def create_duty(route_id, bus_id, driver_id, start_dt, end_dt, working_hours, is_linked, status):
    """Creates a duty_schedules row (a dated, historical record of this
    dispatch action), then:
      - updates buses.current_driver_id/route_id to match — which is what
        actually fires fn_log_driver_swap() and writes a
        driver_assignments_history row (driverweb.py's INSERT never
        touched buses at all, so that trigger never fired for it), and
      - updates bus_drivers.shift_start_time/shift_end_time to this shift's
        time-of-day, making it the driver's new STANDING daily shift —
        i.e. what get_driver_standing_assignment() reports every day going
        forward, until the next dispatch changes it again. Nothing here
        generates a duty_schedules row for future days; the standing
        assignment is what naturally "continues" without one.
    Returns the new duty_id, or None if the DB isn't reachable or it failed.
    """
    conn = _get_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO duty_schedules
                    (route_id, bus_id, driver_id, start_time, end_time, working_hours, is_linked, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING duty_id
                """,
                (route_id, bus_id, driver_id, start_dt, end_dt, working_hours, is_linked, status),
            )
            duty_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE buses SET current_driver_id = %s, route_id = %s WHERE bus_id = %s",
                (driver_id, route_id, bus_id),
            )
            if driver_id is not None:
                cur.execute(
                    "UPDATE bus_drivers SET shift_start_time = %s, shift_end_time = %s WHERE driver_id = %s",
                    (start_dt.time(), end_dt.time(), driver_id),
                )
        conn.commit()
        return duty_id
    except Exception:
        conn.rollback()
        return None


def get_driver_standing_assignment(driver_id):
    """The driver's CURRENT bus/route and recurring daily shift window —
    i.e. "their schedule until reassigned" — derived from buses.route_id/
    current_driver_id plus bus_drivers.shift_start_time/shift_end_time
    (both kept current by create_duty). Returns None if this driver isn't
    currently assigned to any bus."""
    conn = _get_connection()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bus_number, r.route_name, r.route_code,
                       d.shift_start_time, d.shift_end_time
                FROM bus_drivers d
                JOIN buses b ON b.current_driver_id = d.driver_id
                LEFT JOIN bus_routes r ON r.route_id = b.route_id
                WHERE d.driver_id = %s
                """,
                (driver_id,),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        return None
    if row is None:
        return None
    bus_number, route_name, route_code, shift_start, shift_end = row
    return {
        "bus_number": bus_number, "route_name": route_name, "route_code": route_code,
        "shift_start_time": shift_start, "shift_end_time": shift_end,
    }


def get_driver_current_routes(driver_id):
    """Every route currently assigned to this driver, one entry per bus that
    has them as current_driver_id. Normally just one (get_driver_standing_
    assignment covers that single-value case for the shift-time text), but
    nothing in the schema stops a dispatcher leaving a driver as
    current_driver_id on more than one bus, so this returns however many
    there actually are — used to draw the driver's own route(s) on a map."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bus_id, b.bus_number, r.route_id, r.route_name, r.route_code
                FROM buses b
                JOIN bus_routes r ON r.route_id = b.route_id
                WHERE b.current_driver_id = %s
                """,
                (driver_id,),
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [
        {"bus_id": bid, "bus_number": bnum, "route_id": rid, "route_name": rname, "route_code": rcode}
        for bid, bnum, rid, rname, rcode in rows
    ]


def get_driver_schedule(driver_id):
    """A driver's dated duty_schedules history, most recent first — a log
    of past dispatch actions. Their CURRENT/ongoing schedule is
    get_driver_standing_assignment(); this is supplementary history."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ds.duty_id, r.route_name, r.route_code, b.bus_number,
                       ds.start_time, ds.end_time, ds.working_hours, ds.is_linked, ds.status
                FROM duty_schedules ds
                JOIN bus_routes r ON r.route_id = ds.route_id
                JOIN buses b ON b.bus_id = ds.bus_id
                WHERE ds.driver_id = %s
                ORDER BY ds.start_time DESC
                """,
                (driver_id,),
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [
        {
            "duty_id": duty_id, "route_name": route_name, "route_code": route_code, "bus_number": bus_number,
            "start_time": start_time, "end_time": end_time, "working_hours": working_hours,
            "is_linked": is_linked, "status": status,
        }
        for duty_id, route_name, route_code, bus_number, start_time, end_time, working_hours, is_linked, status in rows
    ]


def list_all_duties():
    """The dispatcher's master duty table across every route/bus/driver."""
    conn = _get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ds.duty_id, r.route_name, b.bus_number,
                       COALESCE(d.full_name, 'UNASSIGNED') AS driver_name,
                       ds.working_hours, ds.is_linked, ds.status, ds.start_time, ds.end_time
                FROM duty_schedules ds
                LEFT JOIN bus_routes  r ON r.route_id = ds.route_id
                LEFT JOIN buses       b ON b.bus_id = ds.bus_id
                LEFT JOIN bus_drivers d ON d.driver_id = ds.driver_id
                ORDER BY ds.duty_id DESC
                """
            )
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        return []
    return [
        {
            "duty_id": duty_id, "route_name": route_name, "bus_number": bus_number, "driver_name": driver_name,
            "working_hours": working_hours, "is_linked": is_linked, "status": status,
            "start_time": start_time, "end_time": end_time,
        }
        for duty_id, route_name, bus_number, driver_name, working_hours, is_linked, status, start_time, end_time in rows
    ]
