-- ============================================================
-- Wipes every table and reseeds a small, deliberately Chennai,
-- FULLY interconnected demo dataset with correct foreign keys
-- throughout: routes -> route_stops -> stops, buses -> routes
-- + drivers, app_users -> drivers, duty_schedules -> route +
-- bus + driver.
--
-- This also fixes a real data bug found while inspecting the
-- original seed data: its route_stops rows used route_id values
-- (800001-800010) that matched NO row in bus_routes (which only
-- had route_id 1-8) -- i.e. every route_stops row was orphaned
-- and the Dispatch tab's stop list was empty for every route.
-- This script's route_stops always reference a route_id that was
-- actually just inserted into bus_routes in this same script.
--
-- Safe to re-run: TRUNCATE ... RESTART IDENTITY resets every
-- serial sequence to 1 first, so IDs come out identical each time.
-- ============================================================

TRUNCATE app_users, duty_schedules, driver_assignments_history,
         route_stops, buses, bus_stops, bus_routes, bus_drivers
         RESTART IDENTITY CASCADE;

ALTER SEQUENCE IF EXISTS custom_stop_id_seq RESTART WITH 1;

-- ---------- bus_stops (10 real Chennai locations, stop_id 1-10) ----------
-- bus_stops.stop_id has no default/sequence in this schema (real data is
-- meant to hold OSM node IDs) -- assigned explicitly here, small and easy
-- to reference from route_stops below.
INSERT INTO bus_stops (stop_id, stop_name, operator, source, geom) VALUES
    (1,  'T Nagar (Panagal Park)',          'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2337, 13.0410), 4326)),
    (2,  'Egmore Railway Station',          'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2609, 13.0773), 4326)),
    (3,  'Chennai Central',                 'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2707, 13.0827), 4326)),
    (4,  'Guindy',                          'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2206, 13.0067), 4326)),
    (5,  'Adyar',                           'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2565, 13.0012), 4326)),
    (6,  'Tambaram',                        'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.1198, 12.9246), 4326)),
    (7,  'Koyambedu (CMBT)',                'CMDA','manual-verified', ST_SetSRID(ST_MakePoint(80.2057, 13.0674), 4326)),
    (8,  'Velachery',                       'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2206, 12.9756), 4326)),
    (9,  'Anna Nagar Roundtana',            'MTC', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2101, 13.0850), 4326)),
    (10, 'Broadway Bus Terminus (George Town)', 'MTC/CMDA', 'manual-verified', ST_SetSRID(ST_MakePoint(80.2847, 13.0923), 4326));

-- ---------- bus_routes (3 routes, route_id auto 1-3) ----------
-- path built the exact same way db.py's save_route_to_db does: WKT
-- LINESTRING in lon/lat order, cast to geography.
INSERT INTO bus_routes (route_name, route_code, path) VALUES
    ('T Nagar - Central Express', 'TNC-EXP',
     ST_GeomFromText('LINESTRING(80.2337 13.0410, 80.2609 13.0773, 80.2707 13.0827)', 4326)::geography),
    ('Koyambedu - Broadway Loop', 'KYB-BWY',
     ST_GeomFromText('LINESTRING(80.2057 13.0674, 80.2101 13.0850, 80.2609 13.0773, 80.2847 13.0923)', 4326)::geography),
    ('Tambaram - Guindy Line', 'TMB-GDY',
     ST_GeomFromText('LINESTRING(80.1198 12.9246, 80.2206 12.9756, 80.2206 13.0067, 80.2565 13.0012)', 4326)::geography);

-- ---------- route_stops (each route_id below is a REAL bus_routes.route_id) ----------
INSERT INTO route_stops (route_id, stop_id, stop_sequence, direction) VALUES
    -- Route 1: T Nagar - Central Express (stops 1 -> 2 -> 3)
    (1, 1, 1, 0), (1, 2, 2, 0), (1, 3, 3, 0),
    -- Route 2: Koyambedu - Broadway Loop (stops 7 -> 9 -> 2 -> 10)
    (2, 7, 1, 0), (2, 9, 2, 0), (2, 2, 3, 0), (2, 10, 4, 0),
    -- Route 3: Tambaram - Guindy Line (stops 6 -> 8 -> 4 -> 5)
    (3, 6, 1, 0), (3, 8, 2, 0), (3, 4, 3, 0), (3, 5, 4, 0);

-- ---------- bus_drivers (3 drivers, driver_id auto 1-3) ----------
INSERT INTO bus_drivers (full_name, license_number, phone, hire_date, shift_status, shift_start_time, shift_end_time, emergency_contact, min_rest_hours, max_daily_hours) VALUES
    ('Karthik Subramaniam', 'TN-DL-100234', '9840012345', '2019-05-10', 'ON_DUTY',  '06:00', '14:00', '9840099001', 8.0, 8.0),
    ('Lakshmi Narayanan',   'TN-DL-100235', '9840012346', '2020-08-15', 'OFF_DUTY', '07:00', '15:00', '9840099002', 8.0, 8.0),
    ('Ravi Kumar',          'TN-DL-100236', '9840012347', '2018-02-20', 'ON_BREAK', '09:00', '17:00', '9840099003', 8.0, 8.0);

-- ---------- buses (4 buses, bus_id auto 1-4) ----------
-- Inserting current_driver_id NOT NULL here fires fn_log_driver_swap()'s
-- INSERT branch for real, automatically writing the matching
-- driver_assignments_history rows -- no manual insert needed there.
INSERT INTO buses (bus_number, capacity, model, status, route_id, current_driver_id) VALUES
    ('TN-01-BUS-101', 40, 'Tata Marcopolo',       'ACTIVE',      1, 1),
    ('TN-01-BUS-102', 45, 'Ashok Leyland Viking',  'ACTIVE',      2, 2),
    ('TN-01-BUS-103', 35, 'Volvo B7R',             'MAINTENANCE', 3, NULL),
    ('TN-01-BUS-104', 40, 'Tata Marcopolo',        'ACTIVE',      NULL, NULL);

-- ---------- app_users (3 driver logins + 1 dispatcher) ----------
-- password for all four is 'hackathon2026' (same bcrypt hash used by
-- schema/seed_app_users.py previously -- verified against auth.py earlier).
INSERT INTO app_users (username, password_hash, role, driver_id) VALUES
    ('driver1',                      '$2b$12$SYQ0KpXyXnrqdHvyTvIWhOn6lqRe/Hgucd4bLnw7YoQ6DhjWBguJO', 'DRIVER', 1),
    ('driver2',                      '$2b$12$SYQ0KpXyXnrqdHvyTvIWhOn6lqRe/Hgucd4bLnw7YoQ6DhjWBguJO', 'DRIVER', 2),
    ('driver3',                      '$2b$12$SYQ0KpXyXnrqdHvyTvIWhOn6lqRe/Hgucd4bLnw7YoQ6DhjWBguJO', 'DRIVER', 3),
    ('dispatcher1',                  '$2b$12$SYQ0KpXyXnrqdHvyTvIWhOn6lqRe/Hgucd4bLnw7YoQ6DhjWBguJO', 'DISPATCHER', NULL);

-- ---------- duty_schedules (1 real end-to-end sample duty) ----------
-- Ties route 1 + bus 1 (TN-01-BUS-101) + driver 1 (Karthik) together for
-- today, 9am-5pm -- proves the full join chain (duty_schedules -> bus_routes
-- + buses + bus_drivers) actually returns something sensible.
INSERT INTO duty_schedules (route_id, bus_id, driver_id, start_time, end_time, working_hours, is_linked, status) VALUES
    (1, 1, 1, (CURRENT_DATE + TIME '09:00'), (CURRENT_DATE + TIME '17:00'), 8.00, true, 'SCHEDULED');
