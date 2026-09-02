import streamlit as st
import folium
from folium import DivIcon
from streamlit_folium import st_folium
import requests
import math
import re
from datetime import datetime, date, timedelta

import db    # PostGIS persistence layer — see db.py. Safe to import with no
             # Postgres installed at all; every call in it no-ops until enabled.
import auth  # Login for the Dispatch / Driver Schedule tabs — see auth.py.
             # Same import-safe posture as db.py.

# Flip this on once real Postgres + PostGIS is reachable (set DATABASE_URL,
# or edit the default at the top of db.py) — nothing else needs to change.
# This also gates the Dispatch / Driver Schedule / Drivers tabs and the
# login gate entirely — Build Routes / View Routes keep working standalone
# (in-memory only) with this off, exactly as before.
DB_INTEGRATION_ENABLED = True

st.set_page_config(layout="wide")

user = None
if DB_INTEGRATION_ENABLED:
    user = auth.login_gate()  # renders a login form and st.stop()s until signed in

st.title("Chennai Bus Network Builder")

if DB_INTEGRATION_ENABLED and "db_extras_ensured" not in st.session_state:
    db.ensure_extras()
    st.session_state.db_extras_ensured = True

if "routes" not in st.session_state:
    st.session_state.routes = {}

# Pull in routes already saved in Postgres (from a previous session, or
# seeded directly via SQL) so they actually show up instead of only ever
# existing in whatever browser tab built them. Only runs once per session —
# routes saved during this session are added straight to session_state
# already, no need to reload.
if DB_INTEGRATION_ENABLED and "routes_loaded_from_db" not in st.session_state:
    loaded = db.load_all_routes_from_db()
    if loaded:
        st.session_state.routes.update(loaded)
    st.session_state.routes_loaded_from_db = True

if "builder_points" not in st.session_state:
    st.session_state.builder_points = []
if "last_processed_click" not in st.session_state:
    st.session_state.last_processed_click = None
if "builder_view" not in st.session_state:
    st.session_state.builder_view = {"center": [13.0827, 80.2707], "zoom": 12}
if "view_view" not in st.session_state:
    st.session_state.view_view = {"center": [13.0827, 80.2707], "zoom": 12}


# ---------- OSRM helpers ----------
@st.cache_data(show_spinner=False)
def get_route_sequential(stops_tuple):
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in stops_tuple)
    url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}"
    params = {"overview": "full", "geometries": "geojson"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    route = data["routes"][0]
    coords = [[lat, lon] for lon, lat in route["geometry"]["coordinates"]]
    order = list(range(len(stops_tuple)))
    return coords, route["distance"], route["duration"], order


@st.cache_data(show_spinner=False)
def get_route_optimized(stops_tuple):
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in stops_tuple)
    url = f"http://router.project-osrm.org/trip/v1/driving/{coords_str}"
    params = {
        "source": "first",
        "destination": "last",
        "roundtrip": "false",
        "overview": "full",
        "geometries": "geojson",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    trip = data["trips"][0]
    coords = [[lat, lon] for lon, lat in trip["geometry"]["coordinates"]]
    waypoints_meta = data["waypoints"]
    order = sorted(range(len(stops_tuple)), key=lambda i: waypoints_meta[i]["waypoint_index"])
    return coords, trip["distance"], trip["duration"], order


def compute_route(stops, optimize):
    stops_tuple = tuple(stops)
    if optimize:
        return get_route_optimized(stops_tuple)
    return get_route_sequential(stops_tuple)


def add_numbered_marker(fmap, lat, lon, number, color, popup_text, tooltip=None, opacity=1.0):
    """tooltip shows on HOVER (folium's Marker popup only shows on click) —
    defaults to the same text as popup_text if not given separately.
    opacity < 1 visually dims a marker (used to de-emphasize routes other
    than the one currently selected on the View Routes map)."""
    folium.Marker(
        [lat, lon],
        popup=popup_text,
        tooltip=tooltip if tooltip is not None else popup_text,
        icon=DivIcon(
            icon_size=(28, 28),
            icon_anchor=(14, 14),
            html=f"""
                <div style="
                    background:{color};
                    color:white;
                    border-radius:50%;
                    width:26px;height:26px;
                    display:flex;align-items:center;justify-content:center;
                    font-weight:bold;font-size:12px;
                    border:2px solid white;
                    box-shadow:0 0 3px rgba(0,0,0,0.6);
                    opacity:{opacity};">
                    {number}
                </div>"""
        ),
    ).add_to(fmap)


def stop_label(i, total):
    if i == 0:
        return "Start"
    if i == total - 1:
        return "End"
    return f"Stop {i}"


# ---------- Reverse geocoding (locality names) ----------
_WARD_PATTERN = re.compile(r"^Ward\s*\d+$", re.I)
_ZONE_PREFIX_PATTERN = re.compile(r"^Zone\s*\d+\s*", re.I)


@st.cache_data(show_spinner=False)
def reverse_geocode_locality(lat, lon):
    """A short, human locality/area name for a coordinate (e.g. 'T Nagar',
    'Egattur') — not a full street address. Uses Nominatim (OpenStreetMap)
    reverse geocoding, free and keyless. Returns None if nothing usable comes
    back (so callers can just skip it rather than show 'Unknown').

    Chennai's OSM data tags most points with an administrative "Ward N" /
    "Zone N <name>" rather than the colloquial locality name, so those are
    filtered/cleaned rather than used as-is.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1},
            headers={"User-Agent": "ChennaiBusNetworkBuilder/1.0 (hackathon project)"},
            timeout=8,
        )
        resp.raise_for_status()
        addr = resp.json().get("address", {})
        for key in ("suburb", "neighbourhood", "quarter", "city_district", "town", "village"):
            value = addr.get(key)
            if value and not _WARD_PATTERN.match(value):
                return _ZONE_PREFIX_PATTERN.sub("", value).strip()
        if addr.get("city"):
            return addr["city"]
    except Exception:
        pass
    return None


def locality_suffix(lat, lon):
    """' (T Nagar)' if a locality name is found, else ''."""
    name = reverse_geocode_locality(round(lat, 5), round(lon, 5))
    return f" ({name})" if name else ""


def stop_real_name(r, i, lat, lon):
    """The bare place name (no parens) for stop i of an already-saved route
    r — e.g. for a map marker tooltip. Uses r["stop_names"][i] (computed
    once, at save/load time) instead of a fresh reverse-geocode call —
    matters once there are several saved routes: each fresh call is a live
    Nominatim request, and re-running all of them on every rerun is what
    made the app hang once real saved routes started piling up."""
    names = r.get("stop_names")
    if names and i < len(names) and names[i]:
        return names[i]
    return reverse_geocode_locality(round(lat, 5), round(lon, 5))


def stop_display_suffix(r, i, lat, lon):
    """' (Name)' for stop i of an already-saved route r — see stop_real_name."""
    name = stop_real_name(r, i, lat, lon)
    return f" ({name})" if name else ""


def get_route_stop_names(r):
    """Real stop names for a route, for the View Routes from/to search.
    Prefers r["stop_names"] (set at save time, or from bus_stops.stop_name
    for routes loaded from the DB) — falls back to on-the-fly reverse
    geocoding (cached) only for older routes that predate that field."""
    names = r.get("stop_names")
    if names:
        return [n for n in names if n]
    result = []
    for lat, lon in r["stops"]:
        name = reverse_geocode_locality(round(lat, 5), round(lon, 5))
        if name:
            result.append(name)
    return result


def add_ghost_route(fmap, name, r):
    """Minimal, muted rendering of an already-saved route — grey, unnumbered,
    small dots — so it stays visible for reference while building a new route
    without competing with it visually."""
    folium.PolyLine(
        r["geometry"],
        color="#9e9e9e",
        weight=3,
        dash_array="4,6",
        tooltip=f"{name} (saved)",
    ).add_to(fmap)
    for lat, lon in r["stops"]:
        folium.CircleMarker(
            [lat, lon],
            radius=3,
            color="#9e9e9e",
            weight=1,
            fill=True,
            fill_color="#9e9e9e",
            fill_opacity=0.9,
            tooltip=f"{name} (saved)",
        ).add_to(fmap)


# ---------- Route overlap ----------
STOP_MATCH_TOLERANCE_M = 250  # stops within this distance count as "the same" real-world stop


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(min(1, math.sqrt(a)))


def match_stop_indices(stops_a, stops_b, tolerance_m=STOP_MATCH_TOLERANCE_M):
    """For each stop in stops_a, the index of its nearest stop in stops_b within
    tolerance_m, or None if nothing in stops_b is close enough."""
    matches = []
    for lat_a, lon_a in stops_a:
        best_j, best_d = None, tolerance_m
        for j, (lat_b, lon_b) in enumerate(stops_b):
            d = haversine_m(lat_a, lon_a, lat_b, lon_b)
            if d <= best_d:
                best_j, best_d = j, d
        matches.append(best_j)
    return matches


def overlap_runs(stops_a, stops_b):
    """% of stops_a's own stops that lie on a segment shared with stops_b, plus
    the actual overlapping stretches.

    Overlap requires >=2 CONSECUTIVE stops in stops_a to each match a
    consecutive pair of stops in stops_b (in either direction, since it's the
    same physical road segment either way). A single matching stop with no
    matching neighbor either side is just a coincidental shared point
    (e.g. two routes crossing or briefly touching) — NOT overlap.

    Returns (pct, runs) where runs is a list of (start_idx, end_idx) — inclusive
    index ranges into stops_a marking each maximal continuous overlapping
    stretch (each run spans >=2 stops).
    """
    if len(stops_a) < 2 or len(stops_b) < 2:
        return 0.0, []
    match_map = match_stop_indices(stops_a, stops_b)
    matched_edge = [
        mi is not None and mi1 is not None and abs(mi - mi1) == 1
        for mi, mi1 in zip(match_map, match_map[1:])
    ]
    overlapping_idx = set()
    runs = []
    i, n = 0, len(matched_edge)
    while i < n:
        if matched_edge[i]:
            start = i
            while i < n and matched_edge[i]:
                i += 1
            runs.append((start, i))  # edges [start, i-1] -> stop indices [start, i]
            overlapping_idx.update(range(start, i + 1))
        else:
            i += 1
    pct = 100.0 * len(overlapping_idx) / len(stops_a)
    return pct, runs


def route_overlap_pct(stops_a, stops_b):
    pct, _ = overlap_runs(stops_a, stops_b)
    return pct


def format_overlap_line(name, pct):
    if pct > 0:
        return f"🔁 **{round(pct)}%** overlap with **{name}**"
    return f"⚪ No overlap with **{name}**"


# ---------- Tabs ----------
# A DRIVER only ever sees their own schedule — no map builder, no dispatch,
# no driver roster. A DISPATCHER (or anyone, when DB integration is off —
# there's no login at all in that case) gets the full set. build_tab/
# view_tab are only ever created (and rendered, via render_build_tab()/
# render_view_tab()) in the non-driver branch, so a driver genuinely can't
# reach route building — this isn't just hidden in the UI.
_is_driver = DB_INTEGRATION_ENABLED and user is not None and user["role"] == "DRIVER"
_is_dispatcher = DB_INTEGRATION_ENABLED and user is not None and user["role"] == "DISPATCHER"

if _is_driver:
    (driver_tab,) = st.tabs(["👨‍✈️ My Schedule"])
    build_tab = view_tab = dispatch_tab = drivers_tab = None
else:
    driver_tab = None
    if _is_dispatcher:
        build_tab, view_tab, dispatch_tab, drivers_tab = st.tabs(
            ["🛠️ Build Routes", "🗺️ View Routes", "🎛️ Dispatch", "👥 Drivers"]
        )
    else:
        # DB integration off: no login, no roles — same two tabs as always.
        build_tab, view_tab = st.tabs(["🛠️ Build Routes", "🗺️ View Routes"])
        dispatch_tab = drivers_tab = None

# ===================== BUILD TAB (dispatcher, or DB integration off) =====================
def render_build_tab():
    st.subheader("Add stops")
    st.caption(
        "Click the map to add points in order: first click = start, last click = end, "
        "everything in between = mandatory stops."
    )

    optimize = st.checkbox(
        "Optimize stop order for shortest total distance (TSP)",
        value=False,
        help="Off = visit stops in the order you clicked them. On = keep start/end fixed but reorder middle stops for minimum total distance.",
        key="optimize_toggle",
    )

    col_map, col_panel = st.columns([3, 1])

    with col_map:
        v = st.session_state.builder_view
        builder_map = folium.Map(location=v["center"], zoom_start=v["zoom"], tiles="OpenStreetMap")

        # Already-saved routes, shown minimally (grey, unnumbered) for reference
        for other_name, other_r in st.session_state.routes.items():
            add_ghost_route(builder_map, other_name, other_r)

        n = len(st.session_state.builder_points)
        preview_error = None
        preview_dist = preview_dur = None
        display_order = list(range(n))

        if n >= 2:
            try:
                preview_coords, preview_dist, preview_dur, display_order = compute_route(
                    st.session_state.builder_points, optimize
                )
                folium.PolyLine(preview_coords, color="#9b59b6", weight=5, opacity=0.85).add_to(builder_map)
            except Exception as e:
                preview_error = str(e)
                folium.PolyLine(st.session_state.builder_points, color="gray", weight=3, dash_array="5").add_to(builder_map)

        for visit_pos, orig_idx in enumerate(display_order):
            lat, lon = st.session_state.builder_points[orig_idx]
            label = stop_label(visit_pos, n)
            color = "#2ecc71" if visit_pos == 0 else ("#e74c3c" if visit_pos == n - 1 else "#3498db")
            add_numbered_marker(
                builder_map, lat, lon, visit_pos + 1, color,
                f"{label}: {lat:.4f}, {lon:.4f}{locality_suffix(lat, lon)}",
            )

        map_data = st_folium(
            builder_map,
            width=800,
            height=500,
            key="builder_map",
            returned_objects=["last_clicked", "center", "zoom"],
        )

        # Persist current view so the next rerun doesn't snap back
        if map_data.get("center") is not None:
            st.session_state.builder_view["center"] = [map_data["center"]["lat"], map_data["center"]["lng"]]
        if map_data.get("zoom") is not None:
            st.session_state.builder_view["zoom"] = map_data["zoom"]

        if preview_error:
            st.warning(f"Live routing preview failed, showing straight line instead: {preview_error}")
        elif n >= 2:
            mode = "optimized order" if optimize else "your click order"
            st.caption(f"Live preview ({mode}): {round(preview_dist/1000, 2)} km, ~{round(preview_dur/60, 1)} min estimated")

        clicked = map_data.get("last_clicked")
        if clicked and clicked != st.session_state.last_processed_click:
            st.session_state.builder_points.append((clicked["lat"], clicked["lng"]))
            st.session_state.last_processed_click = clicked
            st.rerun()

    with col_panel:
        st.write("**Stops added (click order):**")
        if not st.session_state.builder_points:
            st.caption("None yet — click the map.")
        else:
            for i, (lat, lon) in enumerate(st.session_state.builder_points):
                st.write(f"{i+1}. {stop_label(i, n)} — `{lat:.4f}, {lon:.4f}`{locality_suffix(lat, lon)}")

        if optimize and n >= 2 and not preview_error:
            st.write("**Actual visiting order (optimized):**")
            for visit_pos, orig_idx in enumerate(display_order):
                lat, lon = st.session_state.builder_points[orig_idx]
                st.write(f"{visit_pos+1}. {stop_label(visit_pos, n)} — `{lat:.4f}, {lon:.4f}`{locality_suffix(lat, lon)}")

        if n >= 2 and st.session_state.routes:
            current_stops = [st.session_state.builder_points[i] for i in display_order]
            overlap_lines = []
            for other_name, other_r in st.session_state.routes.items():
                pct = route_overlap_pct(current_stops, other_r["stops"])
                if pct > 0:
                    overlap_lines.append(format_overlap_line(other_name, pct))
            if overlap_lines:
                st.write("**Overlap with saved routes:**")
                for line in overlap_lines:
                    st.caption(line)
            else:
                st.caption("⚪ No overlap with any saved route.")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Undo last") and st.session_state.builder_points:
                st.session_state.builder_points.pop()
                st.rerun()
        with c2:
            if st.button("Clear all"):
                st.session_state.builder_points = []
                st.session_state.last_processed_click = None
                st.rerun()

        st.divider()
        route_name = st.text_input("Route name", value=f"Route {len(st.session_state.routes) + 1}")
        route_color = st.color_picker("Route color", value="#1f77b4")

        # Link a bus to this route (buses.route_id — a bus's normal/default
        # route). Required, not optional — every route needs a bus behind
        # it. Separate from the Dispatch tab's full shift assignment
        # (driver + time window); this is just the static route<->bus link.
        selected_bus_id = None
        if DB_INTEGRATION_ENABLED:
            all_buses = db.list_all_buses()
            if not all_buses:
                st.warning("No buses in the fleet yet — a bus is required to save a route.")
            else:
                bus_opts = {f"{b['bus_number']} ({b['model']}, {b['status']})": b["bus_id"] for b in all_buses}
                selected_bus_label = st.selectbox("Assign bus to this route", list(bus_opts.keys()))
                selected_bus_id = bus_opts[selected_bus_label]

        if st.button("💾 Save Route", type="primary"):
            if len(st.session_state.builder_points) < 2:
                st.error("Add at least a start and an end point.")
            elif route_name in st.session_state.routes:
                st.error("A route with that name already exists — pick another name.")
            elif DB_INTEGRATION_ENABLED and selected_bus_id is None:
                st.error("Assign a bus to this route before saving.")
            else:
                with st.spinner("Computing route..."):
                    try:
                        coords, distance_m, duration_s, order = compute_route(st.session_state.builder_points, optimize)
                        ordered_stops = [st.session_state.builder_points[i] for i in order]
                        stop_names = [reverse_geocode_locality(round(lat, 5), round(lon, 5)) for lat, lon in ordered_stops]
                        st.session_state.routes[route_name] = {
                            "color": route_color,
                            "stops": ordered_stops,
                            "stop_names": stop_names,
                            "geometry": coords,
                            "distance_km": round(distance_m / 1000, 2),
                            "duration_min": round(duration_s / 60, 1),
                            "optimized": optimize,
                        }
                        if DB_INTEGRATION_ENABLED:
                            db_route_id = db.save_route_to_db(
                                route_name,
                                st.session_state.routes[route_name],
                                locality_lookup=lambda lat, lon: reverse_geocode_locality(round(lat, 5), round(lon, 5)),
                                duration_min=round(duration_s / 60, 1),
                            )
                            st.session_state.routes[route_name]["db_route_id"] = db_route_id
                            if db_route_id is not None and selected_bus_id is not None:
                                db.assign_bus_to_route(selected_bus_id, db_route_id)

                        st.session_state.builder_points = []
                        st.session_state.last_processed_click = None
                        st.success(f"Saved '{route_name}' — {round(distance_m/1000, 2)} km")
                        if DB_INTEGRATION_ENABLED:
                            if db_route_id is not None:
                                st.caption(f"💾 Also saved to PostGIS (route_id={db_route_id})")
                                if selected_bus_id is not None:
                                    st.caption("🚌 Bus assigned to this route")
                            else:
                                st.caption("⚪ PostGIS not reachable — saved locally only")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Routing failed: {e}")

    if st.session_state.routes:
        st.divider()
        st.write("**Saved routes:**")
        for name, r in st.session_state.routes.items():
            c1, c2 = st.columns([5, 1])
            with c1:
                st.write(f"🔵 **{name}** — {r['distance_km']} km, ~{r['duration_min']} min "
                         f"({'optimized order' if r.get('optimized') else 'your order'}), {len(r['stops'])} stops")
                with st.expander("Show stops (visiting order)"):
                    for i, (lat, lon) in enumerate(r["stops"]):
                        st.write(f"{i+1}. {stop_label(i, len(r['stops']))} — `{lat:.4f}, {lon:.4f}`{stop_display_suffix(r, i, lat, lon)}")
            with c2:
                if st.button("Delete", key=f"del_{name}"):
                    if DB_INTEGRATION_ENABLED:
                        db.delete_route_from_db(r.get("db_route_id"))
                    del st.session_state.routes[name]
                    st.rerun()


# ===================== VIEW TAB (dispatcher, or DB integration off) =====================
def match_clicked_route(clicked_tooltip, matching):
    """Given the raw last_object_clicked_tooltip string from st_folium and
    the list of currently-matching route names, returns which route (if
    any) was clicked — matches either the route's own polyline tooltip
    (exact: just the route name) or one of its stop markers' tooltips
    (prefixed "<name> - ", e.g. "<name> - Start: T Nagar")."""
    if not clicked_tooltip:
        return None
    for name in matching:
        if clicked_tooltip == name or clicked_tooltip.startswith(name + " - "):
            return name
    return None


def render_route_detail_block(name, r):
    """Stops / overlap / bus-assignment for one route — the same block
    whether it's being shown because it's the only one selected, or as
    part of "show all matching routes"."""
    st.write(f"**{name}** — {r['distance_km']} km, ~{r['duration_min']} min, {len(r['stops'])} stops")
    with st.expander(f"Stops on {name} (visiting order)"):
        for i, (lat, lon) in enumerate(r["stops"]):
            st.write(f"{i+1}. {stop_label(i, len(r['stops']))} — `{lat:.4f}, {lon:.4f}`{stop_display_suffix(r, i, lat, lon)}")

    others = {k: v for k, v in st.session_state.routes.items() if k != name}
    overlap_lines = []
    for other_name, other_r in others.items():
        pct = route_overlap_pct(r["stops"], other_r["stops"])
        if pct > 0:
            overlap_lines.append(format_overlap_line(other_name, pct))
    with st.expander(f"🔁 Overlap of {name} with other routes"):
        if not others:
            st.caption("No other saved routes to compare against yet.")
        elif overlap_lines:
            for line in overlap_lines:
                st.write(line)
        else:
            st.caption("⚪ No overlap with any other route.")

    # Edit the bus assigned to this route (bus assignment only — driver
    # assignment happens through a proper shift in the Dispatch tab, not
    # as a raw reassignment here). Only possible for routes actually saved
    # to Postgres (db_route_id set).
    db_route_id = r.get("db_route_id")
    if DB_INTEGRATION_ENABLED and db_route_id is not None:
        with st.expander(f"🚌 Bus assignment for {name}"):
            all_buses = db.list_all_buses()
            current_buses = [b for b in all_buses if b["route_id"] == db_route_id]
            current_bus = current_buses[0] if current_buses else None

            bus_labels = ["— none —"] + [
                f"{b['bus_number']} ({b['model']}, {b['status']})" for b in all_buses
            ]
            bus_id_by_label = {"— none —": None}
            for b in all_buses:
                bus_id_by_label[f"{b['bus_number']} ({b['model']}, {b['status']})"] = b["bus_id"]
            current_bus_label = next(
                (lbl for lbl, bid in bus_id_by_label.items()
                 if bid == (current_bus["bus_id"] if current_bus else None)),
                "— none —",
            )
            new_bus_label = st.selectbox(
                "Bus assigned to this route", bus_labels,
                index=bus_labels.index(current_bus_label), key=f"edit_bus_{name}",
            )
            if st.button("Update bus assignment", key=f"update_bus_{name}"):
                new_bus_id = bus_id_by_label[new_bus_label]
                new_bus = next((b for b in all_buses if b["bus_id"] == new_bus_id), None) if new_bus_id is not None else None

                # Clean up stale pairings so nothing dangling is left behind:
                # the bus that WAS on this route (if being replaced) loses
                # both its route and its driver — that driver's shift here
                # is over.
                for b in current_buses:
                    if b["bus_id"] != new_bus_id:
                        db.clear_bus_route(b["bus_id"])
                        db.assign_driver_to_bus(b["bus_id"], None)

                if new_bus_id is not None:
                    # If the incoming bus was pulled off a DIFFERENT route,
                    # its previous driver doesn't carry over either.
                    if new_bus is not None and new_bus["route_id"] is not None and new_bus["route_id"] != db_route_id:
                        db.assign_driver_to_bus(new_bus_id, None)
                    db.assign_bus_to_route(new_bus_id, db_route_id)

                st.success("Bus assignment updated.")
                st.rerun()


def render_view_tab():
    if not st.session_state.routes:
        st.info("No routes saved yet — build one in the 'Build Routes' tab first.")
        return

    if "view_selected_route" not in st.session_state:
        st.session_state.view_selected_route = None
    if "last_processed_view_click" not in st.session_state:
        st.session_state.last_processed_view_click = None

    st.write("**Search by start/end place:**")
    stop_names_by_route = {name: get_route_stop_names(r) for name, r in st.session_state.routes.items()}
    all_stop_names = sorted(set(n for names in stop_names_by_route.values() for n in names))

    col_from, col_to = st.columns(2)
    with col_from:
        from_place = st.selectbox("From (route passes through)", ["Any"] + all_stop_names, key="search_from")
    with col_to:
        to_place = st.selectbox("To (route passes through)", ["Any"] + all_stop_names, key="search_to")

    matching = [
        name for name in st.session_state.routes
        if (from_place == "Any" or from_place in stop_names_by_route[name])
        and (to_place == "Any" or to_place in stop_names_by_route[name])
    ]

    if not matching:
        st.error(
            f"🚫 No routes found — no saved route passes through both "
            f"'{from_place}' and '{to_place}'."
        )
        return

    # A previous selection that no longer matches the current search
    # (e.g. the filter changed) is treated as no selection at all.
    selected = st.session_state.view_selected_route
    if selected not in matching:
        selected = None
        st.session_state.view_selected_route = None

    if selected:
        st.caption(f"Showing **{selected}** — click it again on the map, or use the button below, to see all {len(matching)} matching routes.")
        if st.button("⬅️ Show all matching routes"):
            st.session_state.view_selected_route = None
            st.rerun()
    else:
        st.caption(f"Showing all {len(matching)} matching route(s) — click one on the map to focus on it.")

    vv = st.session_state.view_view
    view_map = folium.Map(location=vv["center"], zoom_start=vv["zoom"], tiles="OpenStreetMap")

    DESELECTED_GREY = "#9e9e9e"  # same grey used for "ghost" saved routes on the Build tab

    for name in matching:
        r = st.session_state.routes[name]
        is_selected = (selected == name)
        # Nothing selected -> everyone shown in their own color, normal
        # weight. Something selected -> that one full-strength in its own
        # color; everyone else turns grey but stays at FULL opacity (not
        # dimmed/faded — still just as visible, just color-coded as "not
        # the selected one").
        if selected is None:
            weight, color = 5, r["color"]
        elif is_selected:
            weight, color = 6, r["color"]
        else:
            weight, color = 3, DESELECTED_GREY

        folium.PolyLine(r["geometry"], color=color, weight=weight, opacity=0.9, tooltip=name).add_to(view_map)
        total = len(r["stops"])
        for i, (lat, lon) in enumerate(r["stops"]):
            stop_name = stop_real_name(r, i, lat, lon) or "Unknown"
            add_numbered_marker(
                view_map, lat, lon, i + 1, color,
                popup_text=f"{name} - {stop_label(i, total)}: {stop_name}",
                tooltip=f"{name} - {stop_label(i, total)}: {stop_name}",
            )

    view_map_data = st_folium(
        view_map,
        width=1000,
        height=550,
        key="view_map",
        returned_objects=["last_object_clicked_tooltip", "center", "zoom"],
    )

    if view_map_data.get("center") is not None:
        st.session_state.view_view["center"] = [view_map_data["center"]["lat"], view_map_data["center"]["lng"]]
    if view_map_data.get("zoom") is not None:
        st.session_state.view_view["zoom"] = view_map_data["zoom"]

    # Clicking anywhere on a route's line or one of its stop markers selects
    # it (both are tooltipped starting with "<route name> - "); clicking the
    # currently-selected route again deselects it. Guarded against
    # reprocessing the same click on every rerun the same way the Build
    # tab's map-click-to-add-stop already is.
    clicked_tooltip = view_map_data.get("last_object_clicked_tooltip")
    if clicked_tooltip and clicked_tooltip != st.session_state.last_processed_view_click:
        st.session_state.last_processed_view_click = clicked_tooltip
        clicked_route = match_clicked_route(clicked_tooltip, matching)
        if clicked_route is not None:
            if st.session_state.view_selected_route == clicked_route:
                st.session_state.view_selected_route = None
            else:
                st.session_state.view_selected_route = clicked_route
            st.rerun()

    st.write("**Route details:**")
    detail_names = [selected] if selected else matching
    for name in detail_names:
        render_route_detail_block(name, st.session_state.routes[name])


# Both functions are defined by now — actually render into their tabs.
if build_tab is not None:
    with build_tab:
        render_build_tab()
if view_tab is not None:
    with view_tab:
        render_view_tab()

# ===================== DISPATCH TAB (dispatcher only) =====================
# Ported from driverweb.py's "Dispatcher Control Matrix", rewritten against
# the real bus_routes/buses/bus_drivers tables (via the db.* functions
# added for this) instead of driverweb.py's own throwaway routes/buses/crew
# tables, and against the reconciled duty_status values (OVERTIME_REQUIRED,
# not driverweb.py's original SCHEDULED_OVERTIME).
if dispatch_tab is not None:

    @st.dialog("🚀 Assign Driver & Vehicle to Route")
    def open_dispatch_modal(route_id, route_name, route_len_km, route_duration_min, target_date):
        st.caption(f"Configuring shift for **{route_name}** ({route_len_km} km, ~{route_duration_min} min one-way)")

        stops = db.list_route_stops(route_id)
        if stops:
            with st.expander(f"Stops on this route ({len(stops)})"):
                for s in stops:
                    st.write(f"{s['stop_sequence']}. {s['stop_name']} — `{s['lat']:.4f}, {s['lon']:.4f}`")

        col_t1, col_t2 = st.columns(2)
        with col_t1:
            start_t = st.time_input(
                "Shift start time", value=datetime.strptime("09:00", "%H:%M").time(), key="dispatch_start_t"
            )
        with col_t2:
            end_t = st.time_input(
                "Shift end time", value=datetime.strptime("17:00", "%H:%M").time(), key="dispatch_end_t"
            )

        dt_start = datetime.combine(target_date, start_t)
        dt_end = datetime.combine(target_date, end_t)
        if dt_end <= dt_start:
            st.error("End time must be after start time.")
            return
        calculated_hours = round((dt_end - dt_start).total_seconds() / 3600.0, 2)

        if db.route_schedule_conflict(route_id, dt_start, dt_end):
            st.error(
                f"'{route_name}' already has another shift scheduled that overlaps or is within "
                f"30 min of this window — pick a different time or check the Master Duty Schedule below."
            )
            return

        # A "cycle" = one complete round trip (out to the end of the route
        # and back to the start) — a whole number, since a bus can't usefully
        # count a partial trip. round_trip_min assumes the return leg takes
        # the same time as the outbound one.
        round_trip_min = route_duration_min * 2
        if round_trip_min > 0:
            cycles = int((calculated_hours * 60) // round_trip_min)
            leftover_min = round((calculated_hours * 60) - cycles * round_trip_min, 1)
            cycle_note = f" (~{round_trip_min:.0f} min/round trip, ~{leftover_min:.0f} min left over)"
        else:
            cycles = 0
            cycle_note = ""
        st.info(
            f"⏱️ Shift duration: `{calculated_hours}` hours\n\n"
            f"🔁 Estimated complete round trips this shift: **{cycles}**{cycle_note}"
        )

        availability = db.list_driver_availability(target_date, calculated_hours, new_start=dt_start, new_end=dt_end)
        if not availability:
            st.warning("No drivers found.")
            return
        # A driver whose existing shift overlaps (or is within 30 min of)
        # this proposed window genuinely can't take it — excluded outright,
        # same as list_available_buses already excludes a booked bus,
        # rather than shown-then-blocked. Capacity (over daily hours) stays
        # a soft warning shown in the label, not a hard exclusion.
        selectable = [a for a in availability if a["schedule_ok"]]
        if not selectable:
            st.warning("No drivers available — everyone has a shift that overlaps or is within 30 min of this window.")
            return
        driver_opts = {}
        for a in selectable:
            status_flag = "✅ Within capacity" if a["capacity_ok"] else "🚨 Over daily hours"
            label = f"{a['full_name']} [{status_flag} | Worked: {a['worked_today']}/{a['max_daily_hours']}h]"
            driver_opts[label] = a
        selected_driver_label = st.selectbox("Assign driver", list(driver_opts.keys()), key="dispatch_driver_select")

        available_buses = db.list_available_buses(dt_start, dt_end)
        if not available_buses:
            st.warning("No buses available for this window — either none are ACTIVE, or all are already booked.")
            return
        bus_opts = {f"{b['bus_number']} ({b['model']})": b for b in available_buses}
        selected_bus_label = st.selectbox("Assign vehicle", list(bus_opts.keys()), key="dispatch_bus_select")

        is_linked = st.checkbox("Linked shift (same bus all shift)", value=True, key="dispatch_is_linked")

        if st.button("Confirm shift dispatch", use_container_width=True, type="primary", key="dispatch_confirm"):
            driver = driver_opts[selected_driver_label]
            bus = bus_opts[selected_bus_label]
            status = "SCHEDULED" if driver["capacity_ok"] else "OVERTIME_REQUIRED"
            duty_id = db.create_duty(
                route_id, bus["bus_id"], driver["driver_id"], dt_start, dt_end, calculated_hours, is_linked, status
            )
            if duty_id is not None:
                st.success(
                    f"Shift assigned to {driver['full_name']} (duty #{duty_id}) — "
                    f"this is now their standing daily shift until reassigned."
                )
                st.rerun()
            else:
                st.error("Could not save the duty — check the database connection.")

    with dispatch_tab:
        st.subheader("🎛️ Dispatch")
        st.caption("Assign a driver and bus to a route built in the Build Routes tab.")

        dispatch_date = st.date_input("Target dispatching date", value=date.today(), key="dispatch_date")
        st.divider()

        col_matrix_left, col_matrix_right = st.columns([1.6, 1.0])

        with col_matrix_left:
            st.subheader("📍 Saved Routes (click to dispatch)")
            routes = db.list_dispatchable_routes()
            if not routes:
                st.info("No routes saved yet — build one in the Build Routes tab first.")
            for r in routes:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([2, 1, 1])
                    with c1:
                        st.markdown(f"#### 🚌 {r['route_name']} (`{r['route_code']}`)")
                        st.caption(f"{r['length_km']} km • ~{r['duration_min']} min one-way • {r['stop_count']} stops")
                    with c2:
                        st.info("Saved")
                    with c3:
                        if st.button("🚀 Assign Shift", key=f"dispatch_btn_{r['route_id']}", use_container_width=True):
                            open_dispatch_modal(r["route_id"], r["route_name"], r["length_km"], r["duration_min"], dispatch_date)

        with col_matrix_right:
            st.subheader("📊 Driver Capacity")
            availability = db.list_driver_availability(dispatch_date, shift_hours=0.0)
            for a in availability:
                with st.container(border=True):
                    badge = "✅ Within capacity" if a["capacity_ok"] else "🚨 Over daily hours"
                    st.markdown(f"**{a['full_name']}** — {badge}")
                    st.caption(f"Worked today: {a['worked_today']} / {a['max_daily_hours']} hours")
                    st.progress(min(a["worked_today"] / a["max_daily_hours"], 1.0) if a["max_daily_hours"] else 0.0)

        st.divider()
        st.subheader("📋 Master Duty Schedule")
        duties = db.list_all_duties()
        if duties:
            st.dataframe(
                [
                    {
                        "Duty ID": d["duty_id"],
                        "Route": d["route_name"],
                        "Bus": d["bus_number"],
                        "Driver": d["driver_name"],
                        "Hours": d["working_hours"],
                        "Type": "🔗 LINKED" if d["is_linked"] else "🔓 UNLINKED",
                        "Status": d["status"],
                        "Start": d["start_time"].strftime("%Y-%m-%d %H:%M") if d["start_time"] else "N/A",
                        "End": d["end_time"].strftime("%Y-%m-%d %H:%M") if d["end_time"] else "N/A",
                    }
                    for d in duties
                ],
                use_container_width=True,
            )
        else:
            st.caption("No duties scheduled yet.")

# ===================== DRIVERS TAB (dispatcher only) =====================
# A full roster: every driver, their current bus/route assignment, their
# standing shift and rest/hours limits, and their dispatch history with a
# running total of hours worked — everything in one place instead of
# hunting across Dispatch's per-shift view.
if drivers_tab is not None:
    with drivers_tab:
        st.subheader("👥 Drivers")
        st.caption("Every driver, their current assignment, and their schedule.")

        overview = db.list_drivers_overview()
        if not overview:
            st.info("No drivers found.")
        for d in overview:
            with st.container(border=True):
                st.markdown(f"### {d['full_name']}")
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.caption("License / phone")
                    st.write(f"{d['license_number']} • {d['phone']}")
                with c2:
                    st.caption("Standing shift")
                    st.write(f"{d['shift_status']} • {d['shift_start_time']}–{d['shift_end_time']}")
                    st.caption("Rest / max daily")
                    st.write(f"{d['min_rest_hours']}h rest, {d['max_daily_hours']}h/day")
                with c3:
                    st.caption("Currently assigned to")
                    if d["bus_number"]:
                        st.write(f"🚌 {d['bus_number']} → {d['route_name'] or '(no route)'}")
                        if st.button("🗑️ Remove shift", key=f"remove_shift_{d['driver_id']}"):
                            if db.remove_driver_shift(d["driver_id"]):
                                st.success(f"Removed {d['full_name']}'s shift.")
                                st.rerun()
                            else:
                                st.error("Could not remove the shift — check the database connection.")
                    else:
                        st.write("— unassigned —")

                schedule = db.get_driver_schedule(d["driver_id"])
                total_hours = sum(float(s["working_hours"]) for s in schedule if s["working_hours"] is not None)
                with st.expander(f"Dispatch history — {len(schedule)} entries, {total_hours:.1f}h total"):
                    if not schedule:
                        st.caption("No dispatch history yet.")
                    else:
                        for s in schedule:
                            st.write(
                                f"- **{s['route_name']}** (`{s['route_code']}`) on `{s['bus_number']}` — "
                                f"{s['start_time'].strftime('%Y-%m-%d %H:%M')} to "
                                f"{s['end_time'].strftime('%Y-%m-%d %H:%M')} "
                                f"({s['working_hours']}h) — {s['status']}"
                            )

# ===================== DRIVER SCHEDULE TAB (driver only) =====================
# Ported from driverweb.py's "Driver Schedule Portal" — the real version
# scopes to the logged-in driver's own driver_id instead of a dropdown that
# could view anyone's schedule (that was only ever a stand-in for auth).
if driver_tab is not None:
    with driver_tab:
        st.subheader("👨‍✈️ My Schedule")
        st.caption(f"🔒 Signed in as **{user['username']}**")
        st.divider()

        standing = db.get_driver_standing_assignment(user["driver_id"])
        current_routes = db.get_driver_current_routes(user["driver_id"])

        if not current_routes:
            st.info("You're not currently assigned to a bus or route.")
        else:
            for cr in current_routes:
                with st.container(border=True):
                    route_bit = f" (`{cr['route_code']}`)" if cr["route_code"] else ""
                    st.markdown(f"### 🚌 {cr['route_name'] or '(no route yet)'}{route_bit}")
                    st.markdown(f"**Vehicle:** `{cr['bus_number']}`")
            if standing is not None:
                st.markdown(
                    f"**Daily shift:** `{standing['shift_start_time']}` – `{standing['shift_end_time']}` "
                    f"— every day until your dispatcher reassigns you"
                )

            # The route(s) actually assigned to this driver, on a map — matched
            # by db_route_id (not name) against the routes already loaded into
            # session_state, since two routes can share a display name (see
            # the "Route 1" / "Route 1 (2)" case).
            st.write("**Your route(s):**")
            driver_map = folium.Map(location=[13.0827, 80.2707], zoom_start=11, tiles="OpenStreetMap")
            any_drawn = False
            for cr in current_routes:
                r = next(
                    (rt for rt in st.session_state.routes.values() if rt.get("db_route_id") == cr["route_id"]),
                    None,
                )
                if r is None:
                    continue
                any_drawn = True
                folium.PolyLine(
                    r["geometry"], color=r["color"], weight=5, opacity=0.9, tooltip=cr["route_name"]
                ).add_to(driver_map)
                total = len(r["stops"])
                for i, (lat, lon) in enumerate(r["stops"]):
                    stop_name = stop_real_name(r, i, lat, lon) or "Unknown"
                    tooltip = f"{cr['route_name']} - {stop_label(i, total)}: {stop_name}"
                    add_numbered_marker(driver_map, lat, lon, i + 1, r["color"], tooltip, tooltip=tooltip)
            if any_drawn:
                st_folium(driver_map, width=1000, height=500, key="driver_route_map")
            else:
                st.caption("Route geometry not available to display.")

        st.divider()
        st.write("**Recent dispatch history:**")
        duties = db.get_driver_schedule(user["driver_id"])
        if not duties:
            st.caption("No dispatch history yet.")
        else:
            for d in duties:
                with st.container(border=True):
                    col1, col2, col3 = st.columns([2, 1.5, 1])
                    with col1:
                        st.markdown(f"### 🚌 {d['route_name']} (`{d['route_code']}`)")
                        st.markdown(f"**Assigned vehicle:** `{d['bus_number']}`")
                    with col2:
                        st.markdown("🕒 **Shift timings**")
                        st.markdown(f"**Start:** `{d['start_time'].strftime('%Y-%m-%d %H:%M')}`")
                        st.markdown(f"**End:** `{d['end_time'].strftime('%Y-%m-%d %H:%M')}`")
                        st.markdown(f"**Working hours:** `{d['working_hours']}`")
                    with col3:
                        duty_label = "🔗 LINKED (same vehicle)" if d["is_linked"] else "🔓 UNLINKED (vehicle swap)"
                        st.info(f"**Duty type:**\n{duty_label}")
                        st.success(f"**Status:** {d['status']}")
