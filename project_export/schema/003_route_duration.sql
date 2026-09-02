-- ============================================================
-- Adds a place to persist a route's average one-way travel time
-- (in minutes). Needed for the Dispatch tab's "estimated number
-- of cycles a bus can do during a shift" calculation -- distance
-- alone (ST_Length) can't give travel TIME, only OSRM (or a
-- manual estimate) can, so this has to be stored, not derived.
--
-- Nullable: routes seeded directly via SQL (no OSRM call involved)
-- leave this NULL; list_dispatchable_routes() in db.py falls back
-- to a distance/assumed-speed estimate when it's NULL.
-- ============================================================

ALTER TABLE public.bus_routes
    ADD COLUMN avg_duration_min NUMERIC(6,1);

COMMENT ON COLUMN public.bus_routes.avg_duration_min IS
    'One-way travel time estimate in minutes (from OSRM at save time, when available). NULL for routes seeded without a routing call -- callers should fall back to a distance-based estimate.';
