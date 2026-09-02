-- ============================================================
-- Additive only: 10 more drivers + 8 more buses, all left
-- UNASSIGNED (route_id/current_driver_id NULL) -- ready to
-- assign/reassign through the app. Deliberately not pre-wired
-- to any route: several real routes now exist that were built
-- through the app itself (not this script), so guessing at
-- pairings for them here would risk stepping on real usage.
-- ============================================================

INSERT INTO bus_drivers (full_name, license_number, phone, hire_date, shift_status, shift_start_time, shift_end_time, emergency_contact, min_rest_hours, max_daily_hours) VALUES
    ('Vignesh Raman',    'TN-DL-100237', '9840012348', '2017-11-03', 'OFF_DUTY', '06:00', '14:00', '9840099004', 8.0, 8.0),
    ('Divya Krishnan',   'TN-DL-100238', '9840012349', '2019-04-22', 'OFF_DUTY', '07:00', '15:00', '9840099005', 8.0, 8.0),
    ('Arun Prakash',     'TN-DL-100239', '9840012350', '2020-01-15', 'OFF_DUTY', '08:00', '16:00', '9840099006', 8.0, 8.0),
    ('Meena Sundaram',   'TN-DL-100240', '9840012351', '2016-09-08', 'OFF_DUTY', '09:00', '17:00', '9840099007', 8.0, 8.0),
    ('Suresh Babu',      'TN-DL-100241', '9840012352', '2021-06-30', 'OFF_DUTY', '10:00', '18:00', '9840099008', 8.0, 8.0),
    ('Priyanka Iyer',    'TN-DL-100242', '9840012353', '2018-02-14', 'OFF_DUTY', '11:00', '19:00', '9840099009', 8.0, 8.0),
    ('Ganesh Moorthy',   'TN-DL-100243', '9840012354', '2015-12-01', 'OFF_DUTY', '12:00', '20:00', '9840099010', 8.0, 8.0),
    ('Kavitha Rajan',    'TN-DL-100244', '9840012355', '2022-03-19', 'OFF_DUTY', '13:00', '21:00', '9840099011', 8.0, 8.0),
    ('Naveen Kumar',     'TN-DL-100245', '9840012356', '2017-07-25', 'OFF_DUTY', '14:00', '22:00', '9840099012', 8.0, 8.0),
    ('Deepa Chandran',   'TN-DL-100246', '9840012357', '2020-10-11', 'OFF_DUTY', '15:00', '23:00', '9840099013', 8.0, 8.0);

INSERT INTO buses (bus_number, capacity, model, status, route_id, current_driver_id) VALUES
    ('TN-01-BUS-105', 40, 'Tata Marcopolo',       'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-106', 45, 'Ashok Leyland Viking',  'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-107', 35, 'Volvo B7R',             'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-108', 40, 'Tata Starbus',          'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-109', 45, 'Ashok Leyland Viking',  'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-110', 35, 'Volvo B7R',             'MAINTENANCE', NULL, NULL),
    ('TN-01-BUS-111', 40, 'Tata Starbus',          'ACTIVE',      NULL, NULL),
    ('TN-01-BUS-112', 45, 'Ashok Leyland Viking',  'OUT_OF_SERVICE', NULL, NULL);
