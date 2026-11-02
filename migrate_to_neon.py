
import os
import sqlite3
import psycopg
from dotenv import load_dotenv

load_dotenv()

SQLITE_DB = "tiketi360.db"
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from .env")

# Read the original SQLite database without modifying it.
source = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
source.row_factory = sqlite3.Row

tables = [
    "events",
    "ticket_types",
    "site_settings",
    "bookings",
    "group_members",
    "payment_transactions",
]

def rows(table):
    return [dict(row) for row in source.execute(
        f"SELECT * FROM {table} ORDER BY id"
    )] if table != "site_settings" else [
        dict(row) for row in source.execute(
            "SELECT * FROM site_settings ORDER BY key"
        )
    ]

def insert_many(conn, table, columns, records):
    if not records:
        print(f"{table}: 0 records")
        return

    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"

    values = [
        tuple(record.get(column) for column in columns)
        for record in records
    ]
    conn.cursor().executemany(sql, values)
    print(f"{table}: copied {len(values)} records")

try:
    with psycopg.connect(DATABASE_URL) as target:

        # Never import over existing application records.
        for table in tables:
            if table == "site_settings":
                continue
            count = target.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            if count:
                raise RuntimeError(
                    f"Destination {table} is not empty. "
                    "Migration stopped."
                )

        # Copy events first because other tables reference them.
        insert_many(target, "events", [
            "id", "slug", "name", "description", "venue",
            "event_date", "status", "created_at"
        ], rows("events"))

        # Ticket types depend on events.
        ticket_records = rows("ticket_types")
        for record in ticket_records:
            record["is_active"] = bool(record["is_active"])

        insert_many(target, "ticket_types", [
            "id", "event_id", "code", "name", "price",
            "is_active", "created_at"
        ], ticket_records)

        # Merge settings with the initial Neon settings.
        for record in rows("site_settings"):
            target.execute("""
                INSERT INTO site_settings (key, value, updated_at)
                VALUES (%s, %s, COALESCE(%s::timestamptz, NOW()))
                ON CONFLICT (key)
                DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = EXCLUDED.updated_at
            """, (
                record["key"],
                record["value"],
                record.get("updated_at")
            ))
        print("site_settings: merged source settings")

        # Bookings depend on events.
        booking_records = rows("bookings")
        for record in booking_records:
            record["used"] = bool(record["used"])

        for record in booking_records:
            record["provider_receipt"] = record.pop("mpesa_receipt")

        insert_many(target, "bookings", [
            "id", "reference", "event_id", "full_name", "email",
            "phone", "category", "quantity", "unit_price",
            "total_amount", "payment_status", "checkout_request_id",
            "merchant_request_id", "provider_receipt", "qr_token",
            "used", "created_at"
        ], booking_records)

        # Group members depend on bookings.
        insert_many(target, "group_members", [
            "id", "booking_id", "full_name", "id_number_encrypted",
            "phone", "is_leader", "entry_status", "checked_in_at",
            "created_at"
        ], [
            {**record, "is_leader": bool(record["is_leader"])}
            for record in rows("group_members")
        ])

        # Payment transactions depend on bookings.
        payment_records = rows("payment_transactions")
        for record in payment_records:
            record["provider_receipt"] = record.pop("mpesa_receipt")

        insert_many(target, "payment_transactions", [
            "id", "booking_id", "provider",
            "provider_transaction_id", "status", "amount",
            "currency", "phone", "provider_receipt",
            "request_id", "created_at", "updated_at"
        ], payment_records)

        # Reset identity sequences after importing explicit IDs.
        for table in [
            "events", "ticket_types", "bookings",
            "group_members", "payment_transactions"
        ]:
            target.execute(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE(MAX(id), 1),
                    COUNT(*) > 0
                )
                FROM {table}
            """)

        # Verify imported counts before committing.
        print("\nMIGRATION VERIFICATION")
        for table in tables:
            count = target.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            print(f"{table}: {count} records in Neon")

    print("\nMIGRATION COMPLETED SUCCESSFULLY.")

finally:
    source.close()
