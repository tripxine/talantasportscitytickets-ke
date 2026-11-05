import os, io, uuid, secrets, base64, json, re
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
import qrcode
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, jsonify, abort
from palpluss import PalPlussClient, PalPlussError

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not configured.")
if not ADMIN_PASSWORD:
    raise RuntimeError("ADMIN_PASSWORD is not configured.")

app = Flask(__name__)
app.secret_key = SECRET_KEY

DATABASE_URL = os.getenv("DATABASE_URL")
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "DISABLED").strip().upper()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").strip()

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
    SESSION_COOKIE_SAMESITE="Lax",
)


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10
    )

def init_db():
    required_tables = (
        "events",
        "ticket_types",
        "site_settings",
        "bookings",
        "group_members",
        "payment_transactions"
    )

    with db() as con:
        missing = []
        for table in required_tables:
            result = con.execute(
                "SELECT to_regclass(%s) AS table_name",
                (f"public.{table}",)
            ).fetchone()
            if not result["table_name"]:
                missing.append(table)

        if missing:
            raise RuntimeError(
                "Required Neon tables are missing: " + ", ".join(missing)
            )

    print("Neon database connected. Existing application tables verified.")

def id_cipher():
    key = os.getenv("ID_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ID encryption key is not configured.")
    return Fernet(key.encode())


def encrypt_id_number(value):
    return id_cipher().encrypt(value.encode()).decode()


def decrypt_id_number(value):
    return id_cipher().decrypt(value.encode()).decode()

def now():
    return datetime.now(timezone.utc).isoformat()

def checkout_csrf_token():
    token = session.get("checkout_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["checkout_csrf"] = token
    return token


def validate_checkout_csrf(value):
    expected = session.get("checkout_csrf", "")
    return bool(expected and value and secrets.compare_digest(expected, value))


def normalize_ke_phone(value):
    phone = re.sub(r"[\s().-]", "", str(value or ""))
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if re.fullmatch(r"254(?:7|1)\d{8}", phone):
        return phone
    return None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def get_event(slug):
    with db() as con:
        return con.execute(
            "SELECT * FROM events WHERE slug=%s AND status='published'",
            (slug,)
        ).fetchone()


def get_ticket_types(event_id):
    with db() as con:
        return con.execute("""
            SELECT code, name, price, wave1_price, wave2_price
            FROM ticket_types
            WHERE event_id=%s AND is_active IS TRUE
            ORDER BY id
        """, (event_id,)).fetchall()




def get_package_pricing(event_id):
    with db() as con:
        rows = con.execute("""
            SELECT category_code, wave, package_code,
                   package_price, discount_amount
            FROM ticket_package_pricing
            WHERE event_id=%s AND is_active IS TRUE
        """, (event_id,)).fetchall()

    return [
        {
            "category_code": row["category_code"],
            "wave": row["wave"],
            "package_code": row["package_code"],
            "package_price": row["package_price"],
            "discount_amount": row["discount_amount"]
        }
        for row in rows
    ]


def get_primary_event_slug():
    with db() as con:
        setting = con.execute(
            "SELECT value FROM site_settings WHERE key='primary_event_slug'"
        ).fetchone()
        return setting["value"] if setting else None

def get_booking(reference):
    with db() as con:
        return con.execute("""SELECT b.*, e.name AS event_name, e.venue, e.event_date
            FROM bookings b JOIN events e ON e.id=b.event_id WHERE b.reference=%s""",
            (reference,)).fetchone()

def qr_data_uri(value):
    img = qrcode.make(value)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()

@app.route("/")
def index():
    primary_slug = get_primary_event_slug()

    if primary_slug:
        event = get_event(primary_slug)
        if event:
            return redirect(url_for("book", slug=event["slug"]))

    with db() as con:
        events = con.execute("""
            SELECT * FROM events
            WHERE status='published'
            ORDER BY event_date
        """).fetchall()

    return render_template(
        "index.html",
        events=events,
        payment_mode=PAYMENT_MODE
    )

@app.route("/book/<slug>", methods=["GET", "POST"])
def book(slug):
    event = get_event(slug)
    if not event:
        abort(404)

    ticket_types = get_ticket_types(event["id"])
    package_pricing = get_package_pricing(event["id"])
    is_wave_event = int(event["id"]) == 2
    ticket_by_code = {item["code"]: item for item in ticket_types}

    package_sizes = {
        "SOLO": 1,
        "COUPLE": 2,
        "FAMILY3": 3,
        "FAMILY4": 4,
        "GROUP5": 5,
        "GROUP10": 10
    }

    booking_enabled = True

    if request.method == "POST":
        contact_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        contact_phone = normalize_ke_phone(request.form.get("phone", ""))
        category = request.form.get("category", "").strip()
        package_selection = request.form.get("package", "").strip()

        if package_selection == "FAMILY":
            family_size = request.form.get("family_size", "").strip()
            package = f"FAMILY{family_size}" if family_size in ("3", "4") else ""
        else:
            package = package_selection

        quantity = package_sizes.get(package, 0)

        wave = request.form.get("wave", "").strip().upper() if is_wave_event else None
        pricing_wave = wave if is_wave_event else "LEGACY"

        try:
            leader_index = int(request.form.get("leader_index", "-1"))
        except ValueError:
            leader_index = -1

        members = []
        for i in range(quantity):
            name = request.form.get(f"member_name_{i}", "").strip()
            id_number = request.form.get(f"member_id_{i}", "").strip()
            member_phone = normalize_ke_phone(
                request.form.get(f"member_phone_{i}", "")
            )
            normalized_id = id_number.replace(" ", "").replace("-", "")

            if (
                len(name) < 2
                or not normalized_id.isalnum()
                or not 5 <= len(normalized_id) <= 20
                or not member_phone
            ):
                flash(f"Please check the details for attendee {i + 1}.", "error")
                return redirect(url_for("book", slug=slug))

            members.append((name, id_number, member_phone))

        selected_ticket = ticket_by_code.get(category)

        if (
            len(contact_name) < 2
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not contact_phone
            or quantity not in package_sizes.values()
            or not selected_ticket
            or not 0 <= leader_index < quantity
            or len(members) != quantity
        ):
            flash(
                "Please check your contact details, package, ticket category and group leader.",
                "error"
            )
            return redirect(url_for("book", slug=slug))

        if is_wave_event and wave != "WAVE1":
            flash(
                "Only Wave 1 Early Bird tickets are currently available.",
                "error"
            )
            return redirect(url_for("book", slug=slug))

        if is_wave_event:
            raw_unit_price = selected_ticket.get("wave1_price")
        else:
            raw_unit_price = selected_ticket.get("price")

        if raw_unit_price is None:
            flash(
                "The selected ticket category does not have a base price configured yet.",
                "error"
            )
            return redirect(url_for("book", slug=slug))

        unit_price = int(raw_unit_price)
        base_total = unit_price * quantity

        pricing_rule = next(
            (
                rule for rule in package_pricing
                if rule["category_code"] == category
                and rule["wave"] == pricing_wave
                and rule["package_code"] == package
            ),
            None
        )

        if (
            not pricing_rule
            or (
                pricing_rule["package_price"] is None
                and pricing_rule["discount_amount"] is None
            )
        ):
            flash(
                "This package has not been configured for the selected ticket category and wave yet.",
                "error"
            )
            return redirect(url_for("book", slug=slug))

        if pricing_rule["package_price"] is not None:
            total_amount = int(pricing_rule["package_price"])
            discount = max(0, base_total - total_amount)
        else:
            discount = min(
                int(pricing_rule["discount_amount"]),
                base_total
            )
            total_amount = base_total - discount

        leader_name = members[leader_index][0]
        reference = "TK" + uuid.uuid4().hex[:10].upper()

        with db() as con:
            cursor = con.execute("""
                INSERT INTO bookings(
                    reference, event_id, full_name, email, phone, category,
                    wave, package_code, quantity, unit_price,
                    discount_amount, total_amount, payment_status, created_at
                ) VALUES(
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'UNPAID',%s
                )
                RETURNING id
            """, (
                reference, event["id"], leader_name, email, contact_phone,
                category, wave, package, quantity, unit_price, discount,
                total_amount, now()
            ))

            booking_id = cursor.fetchone()["id"]

            for i, (name, id_number, member_phone) in enumerate(members):
                con.execute("""
                    INSERT INTO group_members(
                        booking_id, full_name, id_number_encrypted, phone,
                        is_leader, entry_status, created_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                """, (
                    booking_id, name, encrypt_id_number(id_number),
                    member_phone, i == leader_index, "PENDING", now()
                ))

        flash("Booking details saved.", "success")
        return redirect(url_for("payment", reference=reference))

    return render_template(
        "book.html",
        event=event,
        ticket_types=ticket_types,
        package_pricing=package_pricing,
        booking_enabled=booking_enabled,
        is_wave_event=is_wave_event
    )


@app.route("/payment/<reference>/palpluss", methods=["POST"])
def palpluss_stk(reference):
    booking = get_booking(reference)
    if not booking:
        abort(404)

    if not validate_checkout_csrf(request.form.get("csrf_token")):
        abort(400)

    if PAYMENT_MODE != "PALPLUSS" or os.getenv("PALPLUSS_LIVE_ENABLED", "0") != "1":
        flash("Online payments are not enabled.", "error")
        return redirect(url_for("payment", reference=reference))

    amount = int(booking["total_amount"] or 0)
    if amount < 1:
        flash("This booking has no payable amount.", "error")
        return redirect(url_for("payment", reference=reference))

    phone = normalize_ke_phone(
        request.form.get("phone", booking["phone"])
    )
    if not phone:
        flash("Enter a valid Kenyan M-PESA number, such as 0712345678.", "error")
        return redirect(url_for("payment", reference=reference))

    callback_url = os.getenv("PALPLUSS_CALLBACK_URL", "").strip()

    if not callback_url:
        flash("Payment callback URL is not configured.", "error")
        return redirect(url_for("payment", reference=reference))

    try:
        # Lock the booking before checking or initiating payment.
        # Concurrent requests for this booking must wait for this transaction.
        with db() as con:
            locked_booking = con.execute("""
                SELECT * FROM bookings
                WHERE id=%s
                FOR UPDATE
            """, (booking["id"],)).fetchone()

            if not locked_booking:
                abort(404)

            if locked_booking["payment_status"] == "PAID":
                return redirect(url_for("ticket", reference=reference))

            if locked_booking["payment_status"] not in ("UNPAID", "FAILED"):
                flash(
                    "This booking cannot start another payment while its status is being checked.",
                    "warning"
                )
                return redirect(url_for("payment", reference=reference))

            if int(locked_booking["total_amount"] or 0) != amount:
                flash("Booking amount changed. Please reload the confirmation page.", "error")
                return redirect(url_for("payment", reference=reference))

            pending = con.execute("""
                SELECT id FROM payment_transactions
                WHERE booking_id=%s
                  AND provider='PALPLUSS'
                  AND status IN ('PENDING','PROCESSING','INITIATING')
                LIMIT 1
            """, (locked_booking["id"],)).fetchone()

            if pending or locked_booking["payment_status"] == "PENDING":
                flash(
                    "A payment request is already pending. Check your phone. Do not pay again.",
                    "warning"
                )
                return redirect(url_for("payment", reference=reference))

            # The provider call happens while the booking row is locked.
            # No second request for this booking can pass the check concurrently.
            result = PalPlussClient().initiate_stk(
                amount, phone, reference, callback_url
            )

            if not isinstance(result, dict):
                raise PalPlussError("PalPluss returned an unexpected payment response.")

            txid = result.get("transactionId") or result.get("transaction_id")
            request_id = result.get("requestId") or result.get("request_id")

            if not txid:
                raise PalPlussError(
                    "PalPluss did not return a transaction ID. Contact the administrator before retrying."
                )

            con.execute("""
                INSERT INTO payment_transactions(
                    booking_id, provider, provider_transaction_id, status,
                    amount, currency, phone, request_id, created_at, updated_at
                ) VALUES(%s,'PALPLUSS',%s,'PENDING',%s,'KES',%s,%s,%s,%s)
            """, (
                locked_booking["id"],
                str(txid),
                amount,
                phone,
                str(request_id) if request_id else None,
                now(),
                now()
            ))

            updated = con.execute("""
                UPDATE bookings
                SET phone=%s,
                    payment_status='PENDING',
                    checkout_request_id=%s,
                    updated_at=%s
                WHERE id=%s
                  AND payment_status <> 'PAID'
                RETURNING id
            """, (
                phone,
                str(request_id or txid),
                now(),
                locked_booking["id"]
            )).fetchone()

            if not updated:
                raise RuntimeError("Booking payment status could not be updated.")

        flash("Payment prompt requested. Check your phone.", "success")

    except PalPlussError as exc:
        flash(str(exc), "error")

    except Exception:
        app.logger.exception(
            "PalPluss STK initiation or local transaction recording failed for booking %s",
            reference
        )
        flash(
            "Payment processing needs attention. Do not retry if you received an M-PESA prompt. Contact the event administrator.",
            "error"
        )

    return redirect(url_for("payment", reference=reference))


@app.route("/payment/palpluss/callback", methods=["POST"])
def palpluss_callback():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False}), 400

    root = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    tx = root.get("transaction") if isinstance(root.get("transaction"), dict) else root
    txid = tx.get("id") or tx.get("transaction_id") or tx.get("transactionId")
    if not txid:
        return jsonify({"ok": False, "message": "Transaction ID missing."}), 400

    txid = str(txid)
    # Read event metadata from the outer payload or nested data.
    event_type = str(
        payload.get("event_type")
        or root.get("event_type")
        or payload.get("event")
        or root.get("event")
        or payload.get("type")
        or root.get("type")
        or "transaction.updated"
    ).strip()

    callback_status = str(
        tx.get("status") or root.get("status") or "unknown"
    ).strip().upper()

    supplied_event_id = (
        payload.get("event_id")
        or payload.get("eventId")
        or root.get("event_id")
        or root.get("eventId")
    )

    event_id = str(
        supplied_event_id
        or f"{txid}:{event_type}:{callback_status}"
    ).strip()

    try:
        # Record the delivery. A retry can resume an event left in RECEIVED.
        with db() as con:
            con.execute("""
                INSERT INTO payment_webhook_events(
                    provider, provider_event_id, provider_transaction_id,
                    event_type, processing_status, payload, received_at
                ) VALUES('PALPLUSS',%s,%s,%s,'RECEIVED',%s::jsonb,%s)
                ON CONFLICT(provider, provider_event_id) DO NOTHING
            """, (event_id, txid, event_type, json.dumps(payload), now()))

        # Avoid unnecessary provider requests for already-finalized deliveries.
        with db() as con:
            previous = con.execute("""
                SELECT processing_status
                FROM payment_webhook_events
                WHERE provider='PALPLUSS' AND provider_event_id=%s
            """, (event_id,)).fetchone()

        terminal_states = {
            "SUCCESS", "REVERSED", "FAILED", "CANCELLED",
            "EXPIRED", "REVIEW"
        }
        if previous and previous["processing_status"] in terminal_states:
            return jsonify({"ok": True, "duplicate": True}), 200

        # Callback data is untrusted; independently query the provider.
        verified = PalPlussClient().get_transaction(txid)
        if not isinstance(verified, dict):
            raise PalPlussError("Provider returned an invalid transaction response.")

        status = str(verified.get("status") or "").upper()
        verified_id = str(
            verified.get("id")
            or verified.get("transaction_id")
            or verified.get("transactionId")
            or ""
        )
        external_ref = str(
            verified.get("external_reference")
            or verified.get("account_reference")
            or verified.get("accountReference")
            or ""
        )

        phone = str(verified.get("phone_number") or verified.get("phone") or "")
        phone = phone.replace(" ", "").replace("-", "").lstrip("+")
        if phone.startswith("0"):
            phone = "254" + phone[1:]

        expected_amount = verified.get("amount")
        try:
            from decimal import Decimal, InvalidOperation
            amount_decimal = Decimal(str(expected_amount))
            if not amount_decimal.is_finite() or amount_decimal != amount_decimal.to_integral_value():
                raise ValueError("Amount is not a whole KES value.")
            amount = int(amount_decimal)
        except (InvalidOperation, ValueError, TypeError):
            raise PalPlussError("Provider returned an invalid transaction amount.")

        currency = str(verified.get("currency") or "").upper()
        receipt = verified.get("mpesa_receipt") or verified.get("receipt")

        with db() as con:
            # Serialize duplicate deliveries for this webhook event.
            event = con.execute("""
                SELECT processing_status
                FROM payment_webhook_events
                WHERE provider='PALPLUSS' AND provider_event_id=%s
                FOR UPDATE
            """, (event_id,)).fetchone()

            if not event:
                raise PalPlussError("Webhook event record not found.")

            if event["processing_status"] in terminal_states:
                return jsonify({"ok": True, "duplicate": True}), 200

            record = con.execute("""
                SELECT * FROM payment_transactions
                WHERE provider='PALPLUSS' AND provider_transaction_id=%s
                FOR UPDATE
            """, (txid,)).fetchone()

            if not record:
                raise PalPlussError("No matching local payment record.")

            booking = con.execute("""
                SELECT * FROM bookings WHERE id=%s FOR UPDATE
            """, (record["booking_id"],)).fetchone()

            if not booking:
                raise PalPlussError("Booking not found.")

            expected_phone = str(record["phone"] or "")
            expected_phone = expected_phone.replace(" ", "").replace("-", "").lstrip("+")
            if expected_phone.startswith("0"):
                expected_phone = "254" + expected_phone[1:]

            valid = (
                bool(verified_id)
                and verified_id == txid
                and external_ref == booking["reference"]
                and amount == int(booking["total_amount"])
                and amount == int(record["amount"])
                and currency == "KES"
                and phone == expected_phone
            )

            if not valid:
                con.execute("""
                    UPDATE payment_transactions
                    SET status='REVIEW',
                        failure_reason=%s,
                        updated_at=%s
                    WHERE id=%s
                """, ("Provider details did not match booking.", now(), record["id"]))
                con.execute("""
                    UPDATE payment_webhook_events
                    SET processing_status='REVIEW', processed_at=%s
                    WHERE provider='PALPLUSS' AND provider_event_id=%s
                """, (now(), event_id))
                return jsonify({"ok": False, "message": "Manual review required."}), 202

            # A late success must never reactivate a reversed booking.
            if booking["payment_status"] == "REVERSED" and status in ("SUCCESS", "COMPLETED"):
                con.execute("""
                    UPDATE payment_transactions
                    SET status='REVIEW',
                        failure_reason=%s,
                        updated_at=%s
                    WHERE id=%s
                """, ("Provider reported success after the booking was reversed.", now(), record["id"]))
                con.execute("""
                    UPDATE payment_webhook_events
                    SET processing_status='REVIEW', processed_at=%s
                    WHERE provider='PALPLUSS' AND provider_event_id=%s
                """, (now(), event_id))
                return jsonify({"ok": False, "message": "Manual review required."}), 202

            if status in ("SUCCESS", "COMPLETED"):
                token = booking["qr_token"] or secrets.token_urlsafe(32)

                con.execute("""
                    UPDATE bookings
                    SET payment_status='PAID',
                        qr_token=%s,
                        provider_receipt=%s,
                        updated_at=%s
                    WHERE id=%s
                """, (token, receipt, now(), booking["id"]))

                con.execute("""
                    INSERT INTO tickets(
                        booking_id, ticket_number, qr_token, status, issued_at
                    ) VALUES(%s,%s,%s,'ACTIVE',%s)
                    ON CONFLICT(booking_id) DO UPDATE SET
                        qr_token=EXCLUDED.qr_token,
                        status='ACTIVE',
                        revoked_at=NULL
                """, (booking["id"], "TKT" + booking["reference"], token, now()))
                final_status = "SUCCESS"

            elif status == "REVERSED":
                con.execute("""
                    UPDATE bookings
                    SET payment_status='REVERSED',
                        qr_token=NULL,
                        provider_receipt=NULL,
                        updated_at=%s
                    WHERE id=%s
                """, (now(), booking["id"]))

                con.execute("""
                    UPDATE tickets
                    SET status='REVOKED', revoked_at=%s
                    WHERE booking_id=%s AND status <> 'REVOKED'
                """, (now(), booking["id"]))
                final_status = "REVERSED"

            elif status in ("FAILED", "CANCELLED", "EXPIRED"):
                if booking["payment_status"] not in ("PAID", "REVERSED"):
                    con.execute("""
                        UPDATE bookings
                        SET payment_status='FAILED', updated_at=%s
                        WHERE id=%s
                    """, (now(), booking["id"]))
                final_status = status

            elif status in ("PENDING", "PROCESSING"):
                final_status = "PENDING"

            else:
                final_status = "REVIEW"

            con.execute("""
                UPDATE payment_transactions
                SET status=%s, provider_receipt=%s, updated_at=%s
                WHERE id=%s
            """, (final_status, receipt, now(), record["id"]))

            con.execute("""
                UPDATE payment_webhook_events
                SET processing_status=%s, processed_at=%s
                WHERE provider='PALPLUSS' AND provider_event_id=%s
            """, (final_status, now(), event_id))

        return jsonify({"ok": True}), 200

    except PalPlussError:
        app.logger.exception("PalPluss callback verification failed")
        return jsonify({"ok": False, "message": "Transaction not verified."}), 202
    except Exception:
        app.logger.exception("PalPluss callback processing failed")
        return jsonify({"ok": False}), 500


@app.route("/payment/<reference>/status", methods=["GET"])
def payment_status(reference):
    booking = get_booking(reference)
    if not booking:
        abort(404)

    paid = (
        booking["payment_status"] == "PAID"
        and bool(booking["qr_token"])
    )

    response = jsonify({
        "status": booking["payment_status"],
        "paid": paid,
        "ticket_url": (
            url_for("ticket_pdf", reference=reference)
            if paid else None
        )
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/payment/<reference>", methods=["GET"])
def payment(reference):
    booking = get_booking(reference)
    if not booking:
        abort(404)

    with db() as con:
        members = con.execute("""
            SELECT full_name, phone, is_leader
            FROM group_members
            WHERE booking_id=%s
            ORDER BY id
        """, (booking["id"],)).fetchall()

    # Browser requests must never mark bookings as paid.
    return render_template(
        "payment.html",
        booking=booking,
        members=members,
        payment_mode=PAYMENT_MODE,
        can_pay=(
            PAYMENT_MODE == "PALPLUSS"
            and os.getenv("PALPLUSS_LIVE_ENABLED", "0") == "1"
            and booking["payment_status"] in ("UNPAID", "FAILED")
            and int(booking["total_amount"] or 0) > 0
        ),
        csrf_token=checkout_csrf_token()
    )


@app.route("/ticket/<reference>")
def ticket(reference):
    booking = get_booking(reference)
    if not booking:
        abort(404)
    if booking["payment_status"] != "PAID" or not booking["qr_token"]:
        return redirect(url_for("payment", reference=reference))

    with db() as con:
        members = con.execute("""
            SELECT full_name, is_leader, entry_status
            FROM group_members
            WHERE booking_id=%s
            ORDER BY id
        """, (booking["id"],)).fetchall()

    verify_url = BASE_URL.rstrip("/") + url_for(
        "verify_ticket", token=booking["qr_token"]
    )
    return render_template(
        "ticket.html",
        booking=booking,
        members=members,
        qr=qr_data_uri(verify_url),
        verify_url=verify_url
    )

@app.route("/ticket/<reference>/pdf")
def ticket_pdf(reference):
    booking = get_booking(reference)
    if not booking:
        abort(404)
    if booking["payment_status"] != "PAID" or not booking["qr_token"]:
        abort(403)

    from reportlab.lib.pagesizes import A5
    from reportlab.lib.utils import ImageReader
    from reportlab.lib.colors import HexColor, white
    from reportlab.pdfgen import canvas
    import tempfile
    import os

    width, height = A5
    path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    qr_path = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name

    stadium_path = os.path.join(app.static_folder, "images", "talanta-stadium.jpeg")
    bayern_path = os.path.join(app.static_folder, "images", "bayern-logo.jpeg")
    ajax_path = os.path.join(app.static_folder, "images", "ajax-logo.jpeg")

    try:
        qr_img = qrcode.make(
            BASE_URL.rstrip("/") + url_for(
                "verify_ticket", token=booking["qr_token"]
            )
        )
        qr_img.save(qr_path)

        c = canvas.Canvas(path, pagesize=A5)

        # Stadium photograph background with a dark readability overlay.
        if os.path.isfile(stadium_path):
            c.drawImage(
                ImageReader(stadium_path), 0, 0,
                width=width, height=height, preserveAspectRatio=False
            )
        else:
            c.setFillColor(HexColor("#10253a"))
            c.rect(0, 0, width, height, fill=1, stroke=0)

        c.saveState()
        c.setFillAlpha(0.78)
        c.setFillColor(HexColor("#071525"))
        c.rect(0, 0, width, height, fill=1, stroke=0)
        c.restoreState()

        # Main ticket panel.
        c.setFillColor(HexColor("#10253a"))
        c.roundRect(17, 17, width - 34, height - 34, 13, fill=1, stroke=0)

        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 19)
        c.drawString(32, height - 53, "TIKETI360")
        c.setFillColor(HexColor("#49d8d0"))
        c.setFont("Helvetica-Bold", 8)
        c.drawString(33, height - 68, "KENYA  |  DIGITAL ENTRY PASS")

        c.setFillColor(HexColor("#85edc6"))
        c.roundRect(width - 91, height - 61, 57, 19, 8, fill=1, stroke=0)
        c.setFillColor(HexColor("#0b392d"))
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(width - 62.5, height - 54, "PAID")

        # Team logos and matchup.
        logo_y = height - 165
        logo_size = 57
        if os.path.isfile(bayern_path):
            c.drawImage(ImageReader(bayern_path), 53, logo_y,
                        width=logo_size, height=logo_size,
                        preserveAspectRatio=True, mask="auto")
        if os.path.isfile(ajax_path):
            c.drawImage(ImageReader(ajax_path), width - 110, logo_y,
                        width=logo_size, height=logo_size,
                        preserveAspectRatio=True, mask="auto")

        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(width / 2, logo_y + 27, "BAYERN")
        c.setFillColor(HexColor("#49d8d0"))
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(width / 2, logo_y + 11, "VS")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 13)
        c.drawCentredString(width / 2, logo_y - 7, "AJAX")

        c.setFillColor(HexColor("#49d8d0"))
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(width / 2, height - 190, "TALANTA STADIUM")

        # Event and holder information.
        c.setStrokeColor(HexColor("#496078"))
        c.line(32, height - 207, width - 32, height - 207)

        details = [
            ("EVENT", str(booking["event_name"])),
            ("TICKET HOLDER / GROUP LEADER", str(booking["full_name"])),
            ("VENUE", str(booking["venue"])),
            ("EVENT DATE & TIME", str(booking["event_date"]).replace("T", " · ")),
            ("TICKET PACKAGE", {
                "SOLO": "Solo",
                "COUPLE": "Couple",
                "FAMILY3": "Family of 3",
                "FAMILY4": "Family of 4",
                "GROUP5": "Group of 5",
                "GROUP10": "Group of 10",
            }.get(booking.get("package_code"), "Legacy booking")),
            ("CATEGORY / QUANTITY", f'{booking["category"]} x {booking["quantity"]}'),
        ]

        if booking["wave"]:
            wave_label = str(booking["wave"]).replace("WAVE", "Wave ")
            details.append(("TICKET WAVE", wave_label))

        details.append(("BOOKING REFERENCE", str(booking["reference"])))

        y = height - 229
        for label, value in details:
            c.setFillColor(HexColor("#9bb0c2"))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(32, y, label[:42])
            c.setFillColor(white)
            c.setFont("Helvetica", 9)
            c.drawString(32, y - 13, value[:66])
            y -= 38

        # Group attendee names, if applicable.
        with db() as con:
            members = con.execute("""
                SELECT full_name, is_leader
                FROM group_members
                WHERE booking_id=%s
                ORDER BY id
            """, (booking["id"],)).fetchall()

        if members:
            c.setFillColor(HexColor("#49d8d0"))
            c.setFont("Helvetica-Bold", 7)
            c.drawString(32, y, "REGISTERED ATTENDEES")
            y -= 13
            c.setFillColor(white)
            c.setFont("Helvetica", 7)
            for member in members[:5]:
                c.drawString(32, y, str(member["full_name"])[:42])
                y -= 11

        # QR is printed on a white panel for reliable scanning.
        qr_size = 105
        qr_x = width - qr_size - 33
        qr_y = 35
        c.setFillColor(white)
        c.roundRect(qr_x - 7, qr_y - 7, qr_size + 14, qr_size + 29,
                    7, fill=1, stroke=0)
        c.drawImage(qr_path, qr_x, qr_y, width=qr_size, height=qr_size)
        c.setFillColor(HexColor("#10253a"))
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(qr_x + qr_size / 2, qr_y - 1,
                            "SCAN TO VERIFY")

        c.setFillColor(HexColor("#49d8d0"))
        c.setFont("Helvetica-Bold", 8)
        c.drawString(32, 53, "PAYMENT CONFIRMED")
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(32, 37, f'KES {int(booking["total_amount"]):,}')

        c.save()
    finally:
        if os.path.exists(qr_path):
            os.unlink(qr_path)

    return send_file(
        path,
        as_attachment=True,
        download_name=f'{booking["reference"]}.pdf',
        mimetype="application/pdf"
    )

@app.route("/verify/<token>")
def verify_ticket(token):
    with db() as con:
        b = con.execute("""
            SELECT b.*, e.name AS event_name, e.venue
            FROM bookings b
            JOIN events e ON e.id = b.event_id
            WHERE b.qr_token = %s
        """, (token,)).fetchone()

        members = []
        if b:
            members = con.execute("""
                SELECT full_name, is_leader, entry_status
                FROM group_members
                WHERE booking_id = %s
                ORDER BY id
            """, (b["id"],)).fetchall()

    if not b:
        return render_template(
            "verify.html", status="INVALID",
            booking=None, members=[]
        )

    if b["payment_status"] != "PAID":
        status = "NOT PAID"
    elif members:
        status = (
            "ALREADY USED"
            if all(m["entry_status"] == "CHECKED_IN" for m in members)
            else "VALID"
        )
    else:
        status = "ALREADY USED" if b["used"] else "VALID"

    return render_template(
        "verify.html",
        status=status,
        booking=b,
        members=members
    )

@app.route("/api/scan", methods=["POST"])
@admin_required
def scan_ticket():
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    action = data.get("action", "lookup")

    if not token:
        return jsonify({"ok": False, "message": "QR token is required."}), 400

    if action not in ("lookup", "checkin"):
        return jsonify({"ok": False, "message": "Unknown scanner action."}), 400

    with db() as con:
        # Serialize scans for this booking to prevent simultaneous entry approvals.
        booking = con.execute("""
            SELECT * FROM bookings
            WHERE qr_token=%s
            FOR UPDATE
        """, (token,)).fetchone()

        if not booking:
            return jsonify({"ok": False, "message": "Invalid ticket."}), 404

        if booking["payment_status"] != "PAID" or not booking["qr_token"]:
            return jsonify({"ok": False, "message": "Payment not confirmed."}), 403

        members = con.execute("""
            SELECT id, full_name, is_leader, entry_status
            FROM group_members
            WHERE booking_id=%s
            ORDER BY id
            FOR UPDATE
        """, (booking["id"],)).fetchall()

        # Legacy single-attendee bookings.
        if not members:
            if booking["used"]:
                return jsonify({
                    "ok": False,
                    "message": "Ticket already scanned.",
                    "legacy": True
                }), 409

            if action == "lookup":
                return jsonify({
                    "ok": True,
                    "legacy": True,
                    "reference": booking["reference"],
                    "name": booking["full_name"],
                    "message": "Valid ticket. Confirm check-in to approve entry."
                })

            updated = con.execute("""
                UPDATE bookings
                SET used=TRUE, updated_at=%s
                WHERE id=%s AND used=FALSE AND payment_status='PAID'
                RETURNING id
            """, (now(), booking["id"])).fetchone()

            if not updated:
                return jsonify({
                    "ok": False,
                    "message": "Ticket has already been scanned or is no longer valid."
                }), 409

            return jsonify({
                "ok": True,
                "legacy": True,
                "message": "ENTRY APPROVED",
                "name": booking["full_name"],
                "reference": booking["reference"]
            })

        if action == "lookup":
            member_list = [{
                "id": member["id"],
                "name": member["full_name"],
                "is_leader": bool(member["is_leader"]),
                "entry_status": member["entry_status"]
            } for member in members]

            checked_in = sum(
                member["entry_status"] == "CHECKED_IN"
                for member in members
            )

            return jsonify({
                "ok": True,
                "group": True,
                "reference": booking["reference"],
                "leader": booking["full_name"],
                "members": member_list,
                "checked_in": checked_in,
                "total_members": len(members),
                "remaining": len(members) - checked_in
            })

        try:
            member_id = int(data.get("member_id", 0))
        except (TypeError, ValueError):
            return jsonify({
                "ok": False,
                "message": "Select a valid group member."
            }), 400

        member = con.execute("""
            SELECT id, full_name, entry_status
            FROM group_members
            WHERE id=%s AND booking_id=%s
            FOR UPDATE
        """, (member_id, booking["id"])).fetchone()

        if not member:
            return jsonify({
                "ok": False,
                "message": "Member does not belong to this booking."
            }), 404

        if member["entry_status"] == "CHECKED_IN":
            return jsonify({
                "ok": False,
                "message": "This attendee has already checked in."
            }), 409

        updated = con.execute("""
            UPDATE group_members
            SET entry_status='CHECKED_IN', checked_in_at=%s
            WHERE id=%s
              AND booking_id=%s
              AND entry_status='PENDING'
            RETURNING id
        """, (now(), member_id, booking["id"])).fetchone()

        if not updated:
            return jsonify({
                "ok": False,
                "message": "Check-in was not recorded. Refresh and try again."
            }), 409

        remaining = con.execute("""
            SELECT COUNT(*) AS total
            FROM group_members
            WHERE booking_id=%s AND entry_status!='CHECKED_IN'
        """, (booking["id"],)).fetchone()["total"]

        if remaining == 0:
            con.execute("""
                UPDATE bookings SET used=TRUE, updated_at=%s
                WHERE id=%s
            """, (now(), booking["id"]))

        return jsonify({
            "ok": True,
            "message": "ENTRY APPROVED",
            "name": member["full_name"],
            "reference": booking["reference"],
            "remaining": remaining,
            "group_complete": remaining == 0
        })


@app.route("/admin", methods=["GET"])
@admin_required
def admin():
    with db() as con:
        bookings=con.execute("""SELECT b.*,e.name AS event_name FROM bookings b JOIN events e ON e.id=b.event_id
            ORDER BY b.id DESC LIMIT 200""").fetchall()
        stats=con.execute("""SELECT COUNT(*) AS orders, COALESCE(SUM(CASE WHEN payment_status='PAID' THEN total_amount ELSE 0 END),0) AS revenue,
            COALESCE(SUM(CASE WHEN payment_status='PAID' THEN quantity ELSE 0 END),0) AS tickets
            FROM bookings""").fetchone()
        events=con.execute("SELECT * FROM events ORDER BY id DESC").fetchall()
    return render_template(
        "admin.html", bookings=bookings, stats=stats, events=events,
        csrf_token=checkout_csrf_token()
    )

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        if not validate_checkout_csrf(request.form.get("csrf_token")):
            abort(400)
        if secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Incorrect admin password.", "error")
    return render_template(
        "admin_login.html",
        csrf_token=checkout_csrf_token()
    )

@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    if not validate_checkout_csrf(request.form.get("csrf_token")):
        abort(400)
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin/events", methods=["POST"])
@admin_required
def create_event():
    if not validate_checkout_csrf(request.form.get("csrf_token")):
        abort(400)
    name=request.form.get("name","").strip()
    venue=request.form.get("venue","").strip()
    date=request.form.get("event_date","").strip()
    description=request.form.get("description","").strip()
    if not name or not venue or not date:
        flash("Event name, venue and date are required.","error")
    else:
        slug=uuid.uuid4().hex[:12]
        with db() as con:
            con.execute("""INSERT INTO events(slug,name,description,venue,event_date,status,created_at)
                VALUES(%s,%s,%s,%s,%s,'published',%s)""",(slug,name,description,venue,date,now()))
        flash("Event created.","success")
    return redirect(url_for("admin"))

@app.route("/admin/events/<int:event_id>/toggle", methods=["POST"])
@admin_required
def toggle_event(event_id):
    if not validate_checkout_csrf(request.form.get("csrf_token")):
        abort(400)
    with db() as con:
        con.execute("UPDATE events SET status=CASE WHEN status='published' THEN 'hidden' ELSE 'published' END WHERE id=%s",(event_id,))
    return redirect(url_for("admin"))

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("FLASK_DEBUG","false").lower()=="true")
