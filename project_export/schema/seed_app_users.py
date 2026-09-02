"""
One-off seeding script: creates an app_users login for every existing
bus_drivers row (role=DRIVER, linked via driver_id) plus a couple of
DISPATCHER accounts, all sharing one demo password — so the merged app
has ready-made logins for a demo without provisioning 25 real credentials
under time pressure.

Run once, after schema/001_dispatch_integration.sql has been applied to
the live Hackatronicsdb:

    python3 schema/seed_app_users.py

Safe to re-run — inserts are ON CONFLICT (username) DO NOTHING.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import db

DEMO_PASSWORD = "hackathon2026"


def main():
    conn = db._get_connection()
    if conn is None:
        print("Could not connect to Postgres (check DATABASE_URL) — nothing seeded.")
        return

    password_hash = auth.hash_password(DEMO_PASSWORD)
    if password_hash is None:
        print("bcrypt isn't installed — install it first: pip install bcrypt")
        return

    created = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT driver_id, full_name FROM bus_drivers ORDER BY driver_id")
            drivers = cur.fetchall()

            for driver_id, full_name in drivers:
                # Short and predictable (driver1, driver2, ...) rather than
                # name-based — easier to type/remember when testing, and
                # obvious what the next username would be for a new test driver.
                username = f"driver{driver_id}"
                cur.execute(
                    """
                    INSERT INTO app_users (username, password_hash, role, driver_id)
                    VALUES (%s, %s, 'DRIVER', %s)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    (username, password_hash, driver_id),
                )
                if cur.rowcount:
                    created.append(username)

            for dispatcher_username in ("dispatcher1", "dispatcher2"):
                cur.execute(
                    """
                    INSERT INTO app_users (username, password_hash, role, driver_id)
                    VALUES (%s, %s, 'DISPATCHER', NULL)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    (dispatcher_username, password_hash),
                )
                if cur.rowcount:
                    created.append(dispatcher_username)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Seeding failed, rolled back: {e}")
        return

    print(f"Seeded {len(created)} new app_users row(s). Demo password for all: {DEMO_PASSWORD!r}")
    for username in created:
        print(f"  - {username}")


if __name__ == "__main__":
    main()
