import os, io, uuid, secrets, base64, json
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
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-before-deployment")
DATABASE_URL = os.getenv("DATABASE_URL")
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "DISABLED").strip().upper()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeMe123!")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000")

EVENT_SEED = {
    "slug": "talanta-demo-football-event",
    "name": "Talanta Stadium Demo Football Event",
    "description": "Demonstration event for testing the TIKETI360 booking and digital-ticket journey. This is not an announcement of an actual match.",
    "venue": "Talanta Sports City (Raila Odinga International Stadium), Nairobi",
    "event_date": "2027-01-05T15:00",
    "status": "demo"
}

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
            SELECT code, name, price
            FROM ticket_types
            WHERE event_id=%s AND is_active IS TRUE AND price IS NOT NULL
            ORDER BY id
        """, (event_id,)).fetchall()


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
    prices = {item["code"]: int(item["price"]) for item in ticket_types}

    # Allow unpaid reservations; payment remains disabled until verified.
    booking_enabled = True

    if request.method == "POST":
        contact_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        contact_phone = request.form.get("phone", "").strip().replace(" ", "")
        category = request.form.get("category", "").strip()
        if not prices:
            category = "PRICE_PENDING"

        package_sizes = {"SOLO": 1, "GROUP5": 5, "GROUP10": 10}
        package = request.form.get("package", "").strip()
        quantity = package_sizes.get(package, 0)

        try:
            leader_index = int(request.form.get("leader_index", "-1"))
        except ValueError:
            leader_index = -1

        members = []
        for i in range(quantity):
            name = request.form.get(f"member_name_{i}", "").strip()
            id_number = request.form.get(f"member_id_{i}", "").strip()
            member_phone = request.form.get(f"member_phone_{i}", "").strip().replace(" ", "")
            normalized_id = id_number.replace(" ", "").replace("-", "")
            digits = member_phone.lstrip("+")

            if (
                len(name) < 2
                or not normalized_id.isalnum()
                or not 5 <= len(normalized_id) <= 20
                or not digits.isdigit()
                or not 9 <= len(digits) <= 15
            ):
                flash(f"Please check the details for attendee {i + 1}.", "error")
                return redirect(url_for("book", slug=slug))

            members.append((name, id_number, member_phone))

        contact_digits = contact_phone.lstrip("+")
        if (
            len(contact_name) < 2
            or "@" not in email
            or not contact_digits.isdigit()
            or not 9 <= len(contact_digits) <= 15
            or quantity not in (1, 5, 10)
            or (bool(prices) and category not in prices)
            or not 0 <= leader_index < quantity
            or len(members) != quantity
        ):
            flash("Please check your contact details, package, ticket category and group leader.", "error")
            return redirect(url_for("book", slug=slug))

        unit_price = prices.get(category, 0)
        discount = 200 * (quantity - 1)
        total_amount = max(0, unit_price * quantity - discount)
        leader_name = members[leader_index][0]
        reference = "TK" + uuid.uuid4().hex[:10].upper()

        with db() as con:
            cursor = con.execute("""
                INSERT INTO bookings(
                    reference, event_id, full_name, email, phone, category,
                    quantity, unit_price, discount_amount, total_amount, created_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                reference, event["id"], leader_name, email, contact_phone,
                category, quantity, unit_price, discount, total_amount, now()
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
        booking_enabled=booking_enabled
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
    if booking["payment_status"] == "PAID":
        return redirect(url_for("ticket", reference=reference))

    amount = int(booking["total_amount"] or 0)
    if amount < 1:
        flash("This booking has no payable amount.", "error")
        return redirect(url_for("payment", reference=reference))

    phone = request.form.get("phone", booking["phone"]).strip()
    callback_url = os.getenv("PALPLUSS_CALLBACK_URL", "").strip()
    if not callback_url:
        flash("Payment callback URL is not configured.", "error")
        return redirect(url_for("payment", reference=reference))

    with db() as con:
        pending = con.execute("""
            SELECT id FROM payment_transactions
            WHERE booking_id=%s AND provider='PALPLUSS'
              AND status IN ('PENDING','PROCESSING')
            LIMIT 1
        """, (booking["id"],)).fetchone()
    if pending:
        flash("A payment request is already pending. Check your phone.", "warning")
        return redirect(url_for("payment", reference=reference))

    try:
        result = PalPlussClient().initiate_stk(
            amount, phone, reference, callback_url
        )
        txid = result.get("transactionId") or result.get("transaction_id")
        request_id = result.get("requestId") or result.get("request_id")
        if not txid:
            raise PalPlussError("PalPluss did not return a transaction ID.")

        with db() as con:
            con.execute("""
                INSERT INTO payment_transactions(
                    booking_id, provider, provider_transaction_id, status,
                    amount, currency, phone, request_id, created_at, updated_at
                ) VALUES(%s,'PALPLUSS',%s,'PENDING',%s,'KES',%s,%s,%s,%s)
            """, (booking["id"], str(txid), amount, phone,
                  str(request_id) if request_id else None, now(), now()))
            con.execute("""
                UPDATE bookings
                SET phone=%s, payment_status='PENDING',
                    checkout_request_id=%s, updated_at=%s
                WHERE id=%s AND payment_status!='PAID'
            """, (phone, str(request_id or txid), now(), booking["id"]))
        flash("Payment prompt requested. Check your phone.", "success")
    except PalPlussError as exc:
        flash(str(exc), "error")
    except Exception:
        app.logger.exception("PalPluss STK initiation failed")
        flash("Payment could not be started. Please contact the event administrator.", "error")

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
    event_type = str(payload.get("event") or payload.get("type") or "transaction.update")
    event_id = str(payload.get("event_id") or payload.get("eventId") or f"{txid}:{event_type}")

    try:
        with db() as con:
            con.execute("""
                INSERT INTO payment_webhook_events(
                    provider, provider_event_id, provider_transaction_id,
                    event_type, processing_status, payload, received_at
                ) VALUES('PALPLUSS',%s,%s,%s,'RECEIVED',%s::jsonb,%s)
                ON CONFLICT(provider, provider_event_id) DO NOTHING
            """, (event_id, txid, event_type, json.dumps(payload), now()))

        # Callback contents are untrusted; independently query PalPluss.
        verified = PalPlussClient().get_transaction(txid)
        status = str(verified.get("status") or "").upper()
        verified_id = str(verified.get("transaction_id") or verified.get("transactionId") or txid)
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
        amount = int(float(verified.get("amount") or 0))
        currency = str(verified.get("currency") or "").upper()
        receipt = verified.get("mpesa_receipt") or verified.get("receipt")

        with db() as con:
            record = con.execute("""
                SELECT * FROM payment_transactions
                WHERE provider='PALPLUSS' AND provider_transaction_id=%s
                FOR UPDATE
            """, (txid,)).fetchone()
            if not record:
                raise PalPlussError("No matching local payment record.")

            booking = con.execute(
                "SELECT * FROM bookings WHERE id=%s FOR UPDATE",
                (record["booking_id"],)
            ).fetchone()
            if not booking:
                raise PalPlussError("Booking not found.")

            expected_phone = str(record["phone"]).replace(" ", "").replace("-", "").lstrip("+")
            if expected_phone.startswith("0"):
                expected_phone = "254" + expected_phone[1:]

            valid = (
                verified_id == txid
                and external_ref == booking["reference"]
                and amount == int(booking["total_amount"])
                and amount == int(record["amount"])
                and currency == "KES"
                and phone == expected_phone
            )
            if not valid:
                con.execute("""
                    UPDATE payment_transactions
                    SET status='REVIEW', failure_reason=%s, updated_at=%s
                    WHERE id=%s
                """, ("Provider details did not match booking.", now(), record["id"]))
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
                    SET payment_status='PAID', qr_token=%s,
                        provider_receipt=%s, updated_at=%s
                    WHERE id=%s
                """, (token, receipt, now(), booking["id"]))
                con.execute("""
                    INSERT INTO tickets(
                        booking_id, ticket_number, qr_token, status, issued_at
                    ) VALUES(%s,%s,%s,'ACTIVE',%s)
                    ON CONFLICT(booking_id) DO NOTHING
                """, (booking["id"], "TKT" + booking["reference"], token, now()))
                final_status = "SUCCESS"
            elif status in ("FAILED", "CANCELLED", "REVERSED", "EXPIRED"):
                if booking["payment_status"] != "PAID":
                    con.execute("""
                        UPDATE bookings SET payment_status='FAILED', updated_at=%s
                        WHERE id=%s
                    """, (now(), booking["id"]))
                final_status = status
            else:
                final_status = "PENDING" if status in ("PENDING", "PROCESSING") else "REVIEW"

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

    return jsonify({
        "status": booking["payment_status"],
        "paid": paid,
        "ticket_url": (
            url_for("ticket_pdf", reference=reference)
            if paid else None
        )
    })


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
            and booking["payment_status"] not in ("PAID", "PENDING")
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
    if not booking: abort(404)
    if booking["payment_status"] != "PAID" or not booking["qr_token"]: abort(403)
    from reportlab.lib.pagesizes import A5
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    import tempfile
    path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    c = canvas.Canvas(path, pagesize=A5)
    w,h=A5
    c.setFillColorRGB(.035,.075,.15); c.rect(0,h-100,w,100,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont("Helvetica-Bold",20); c.drawString(28,h-48,"TIKETI360")
    c.setFont("Helvetica",9); c.drawString(28,h-68,"DIGITAL EVENT TICKET")
    c.setFillColorRGB(.08,.12,.2); c.setFont("Helvetica-Bold",15); c.drawString(28,h-132,booking["event_name"][:42])
    c.setFont("Helvetica",10)
    lines=[("Name",booking["full_name"]),("Venue",booking["venue"]),("Date",booking["event_date"]),
           ("Category",booking["category"]),("Quantity",str(booking["quantity"])),
           ("Reference",booking["reference"]),("Payment",booking["payment_status"])]
    y=h-160
    for label,value in lines:
        c.setFont("Helvetica-Bold",9); c.drawString(28,y,label.upper())
        c.setFont("Helvetica",10); c.drawString(110,y,str(value)[:48]); y-=22
    qr_img=qrcode.make(BASE_URL.rstrip("/") + url_for("verify_ticket",token=booking["qr_token"]))
    qpath=tempfile.NamedTemporaryFile(suffix=".png",delete=False).name
    qr_img.save(qpath)
    c.drawImage(qpath,w-125,35,width=95,height=95)
    c.setFont("Helvetica",7); c.drawString(28,45,"Present this QR code at the authorised event entrance.")
    c.drawString(28,32,"A ticket is valid only after payment confirmation.")
    c.save()
    return send_file(path, as_attachment=True, download_name=f"{booking['reference']}.pdf", mimetype="application/pdf")

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

    with db() as con:
        booking = con.execute(
            "SELECT * FROM bookings WHERE qr_token=%s", (token,)
        ).fetchone()

        if not booking:
            return jsonify({"ok": False, "message": "Invalid ticket."}), 404

        if booking["payment_status"] != "PAID":
            return jsonify({"ok": False, "message": "Payment not confirmed."}), 403

        members = con.execute("""
            SELECT id, full_name, is_leader, entry_status
            FROM group_members
            WHERE booking_id=%s
            ORDER BY id
        """, (booking["id"],)).fetchall()

        # Historical single tickets: lookup must not consume entry.
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

            if action == "checkin":
                con.execute(
                    "UPDATE bookings SET used=TRUE WHERE id=%s AND used=FALSE",
                    (booking["id"],)
                )
                return jsonify({
                    "ok": True,
                    "legacy": True,
                    "message": "ENTRY APPROVED",
                    "name": booking["full_name"],
                    "reference": booking["reference"]
                })

            return jsonify({
                "ok": False,
                "message": "Unknown scanner action."
            }), 400

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

        if action == "checkin":
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

            con.execute("""
                UPDATE group_members
                SET entry_status='CHECKED_IN', checked_in_at=%s
                WHERE id=%s AND booking_id=%s AND entry_status='PENDING'
            """, (now(), member_id, booking["id"]))

            remaining = con.execute("""
                SELECT COUNT(*) AS total
                FROM group_members
                WHERE booking_id=%s AND entry_status!='CHECKED_IN'
            """, (booking["id"],)).fetchone()["total"]

            if remaining == 0:
                con.execute(
                    "UPDATE bookings SET used=TRUE WHERE id=%s",
                    (booking["id"],)
                )

            return jsonify({
                "ok": True,
                "message": "ENTRY APPROVED",
                "name": member["full_name"],
                "reference": booking["reference"],
                "remaining": remaining,
                "group_complete": remaining == 0
            })

        return jsonify({"ok": False, "message": "Unknown scanner action."}), 400


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
    return render_template("admin.html",bookings=bookings,stats=stats,events=events)

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        if secrets.compare_digest(request.form.get("password",""),ADMIN_PASSWORD):
            session["admin"]=True
            return redirect(url_for("admin"))
        flash("Incorrect admin password.","error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin/events", methods=["POST"])
@admin_required
def create_event():
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
    with db() as con:
        con.execute("UPDATE events SET status=CASE WHEN status='published' THEN 'hidden' ELSE 'published' END WHERE id=%s",(event_id,))
    return redirect(url_for("admin"))

@app.route("/health")
def health():
    return jsonify({"status":"ok","payment_mode":PAYMENT_MODE})

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=os.getenv("FLASK_DEBUG","false").lower()=="true")
