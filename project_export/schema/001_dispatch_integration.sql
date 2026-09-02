-- ============================================================
-- Migration: merge driverweb.py's dispatch/driver-schedule
-- concept into the real Hackatronicsdb schema, plus auth.
--
-- Reconciles two teammate files that disagreed with each other:
--   - driverweb.py (its own throwaway routes/buses/crew schema,
--     never applied here) set duty status to 'SCHEDULED_OVERTIME'
--     and 'COMPLETED', and read crew.min_rest_hours/max_daily_hours.
--   - tables.sql (targets the real bus_routes/buses/bus_drivers
--     tables correctly) defined duty_status without either of
--     those two values, and bus_drivers has no rest/hours columns.
-- This migration is tables.sql's approach, kept as the base, with
-- 'COMPLETED' added to duty_status (a real terminal state the app
-- needs) and the two missing bus_drivers columns added. Application
-- code must use 'OVERTIME_REQUIRED', not 'SCHEDULED_OVERTIME' --
-- that rename happens in db.py/test.py, not here.
--
-- Also adds app_users, since driverweb.py has no login/auth at all
-- despite the project needing one.
--
-- Additive only -- does not touch bus_routes/bus_stops/route_stops/
-- buses/bus_drivers' EXISTING columns or data.
-- ============================================================

-- ---------- driver_assignments_history ----------
-- Required by the existing fn_log_driver_swap() trigger on
-- public.buses, which currently has nowhere to insert into and
-- will error on the next INSERT/UPDATE to buses.current_driver_id
-- without this. Confirmed against fn_log_driver_swap()'s actual
-- body (extracted from the Database dump): it inserts exactly
-- these four columns, with change_type as one of the plain-text
-- values 'ASSIGNED' / 'UNASSIGNED' / 'SWAPPED'.
CREATE TABLE public.driver_assignments_history (
    history_id     SERIAL PRIMARY KEY,
    bus_id         INTEGER NOT NULL REFERENCES public.buses(bus_id) ON DELETE CASCADE,
    old_driver_id  INTEGER REFERENCES public.bus_drivers(driver_id) ON DELETE SET NULL,
    new_driver_id  INTEGER REFERENCES public.bus_drivers(driver_id) ON DELETE SET NULL,
    change_type    VARCHAR(20) NOT NULL,
    changed_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX idx_driver_assignments_history_bus_id ON public.driver_assignments_history USING btree (bus_id);

COMMENT ON TABLE public.driver_assignments_history IS 'Audit trail written automatically by trg_log_driver_swap whenever buses.current_driver_id changes.';

-- ---------- duty_status enum ----------
-- tables.sql's values, plus COMPLETED: driverweb.py's seed data
-- and its "finished shift" concept legitimately need a terminal
-- state that isn't CANCELLED, so it's added rather than dropped.
CREATE TYPE public.duty_status AS ENUM (
    'PENDING_ASSIGNMENT',
    'SCHEDULED',
    'OVERTIME_REQUIRED',
    'NEEDS_ATTENTION',
    'GAP_UNCOVERED',
    'OVERRIDDEN',
    'CANCELLED',
    'COMPLETED'
);

-- ---------- duty_schedules ----------
-- One row per shift: which driver ran which bus on which route,
-- and when. References the REAL tables (bus_routes/buses/bus_drivers),
-- not driverweb.py's own throwaway routes/buses/crew tables.
CREATE TABLE public.duty_schedules (
    duty_id       SERIAL PRIMARY KEY,
    route_id      INTEGER NOT NULL REFERENCES public.bus_routes(route_id) ON DELETE RESTRICT,
    bus_id        INTEGER NOT NULL REFERENCES public.buses(bus_id) ON DELETE RESTRICT,
    driver_id     INTEGER REFERENCES public.bus_drivers(driver_id) ON DELETE SET NULL,
    start_time    TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time      TIMESTAMP WITH TIME ZONE NOT NULL,
    working_hours NUMERIC(4,2),
    is_linked     BOOLEAN NOT NULL DEFAULT false,
    status        public.duty_status NOT NULL DEFAULT 'PENDING_ASSIGNMENT',
    CONSTRAINT chk_duty_times CHECK (end_time > start_time)
);

CREATE INDEX idx_duty_schedules_driver_id ON public.duty_schedules USING btree (driver_id);
CREATE INDEX idx_duty_schedules_route_id  ON public.duty_schedules USING btree (route_id);
CREATE INDEX idx_duty_schedules_bus_id    ON public.duty_schedules USING btree (bus_id);
CREATE INDEX idx_duty_schedules_start     ON public.duty_schedules USING btree (start_time);

COMMENT ON TABLE public.duty_schedules IS 'One row per shift: which driver ran which bus on which route, and when.';
COMMENT ON COLUMN public.duty_schedules.driver_id IS 'Nullable -- a duty can exist unassigned (PENDING_ASSIGNMENT) before a driver is picked.';

-- ---------- bus_drivers: rest/hours columns ----------
-- driverweb.py's dispatch UI needs these for its rest-violation and
-- daily-hours-worked checks; they don't exist on the real table.
-- NOT NULL with a default so the existing 25 seeded drivers aren't
-- broken by this migration.
ALTER TABLE public.bus_drivers
    ADD COLUMN min_rest_hours  NUMERIC(4,2) NOT NULL DEFAULT 8.0,
    ADD COLUMN max_daily_hours NUMERIC(4,2) NOT NULL DEFAULT 8.0;

-- ---------- auth ----------
-- No login/auth table exists anywhere in the schema. A DRIVER-role
-- login resolves to exactly one bus_drivers.driver_id (the join key
-- for "my schedule"); DISPATCHER-role rows leave driver_id null.
CREATE TYPE public.app_user_role AS ENUM ('DISPATCHER', 'DRIVER');

CREATE TABLE public.app_users (
    user_id        SERIAL PRIMARY KEY,
    username       VARCHAR(50) UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    role           public.app_user_role NOT NULL,
    driver_id      INTEGER REFERENCES public.bus_drivers(driver_id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ DEFAULT now(),
    CHECK (role <> 'DRIVER' OR driver_id IS NOT NULL)
);
CREATE UNIQUE INDEX app_users_driver_id_uidx ON public.app_users(driver_id) WHERE driver_id IS NOT NULL;

COMMENT ON TABLE public.app_users IS 'Login table for the merged dispatcher/driver app. DRIVER-role rows must link to a bus_drivers row; DISPATCHER-role rows leave driver_id null.';
