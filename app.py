import os
import sqlite3
import re

try:
    import psycopg2
    from psycopg2.extras import DictCursor
except ImportError:
    psycopg2 = None
    DictCursor = None
import secrets
import uuid
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "taskora.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "CHANGE_THIS_IN_PRODUCTION")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"

FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "")
FLW_WEBHOOK_HASH = os.environ.get("FLW_WEBHOOK_HASH", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
ACTIVATION_FEE = 3000
MIN_WITHDRAWAL = 5000
CURRENCY = "NGN"
LAGOS_TZ = ZoneInfo("Africa/Lagos")
FLUTTERWAVE_PAYMENT_LINK = os.environ.get(
    "FLUTTERWAVE_PAYMENT_LINK",
    "https://flutterwave.com/pay/io7rwhtgumk4"
).strip()


class DBConnection:
    """Database adapter: SQLite for local development, PostgreSQL on Render."""
    def __init__(self):
        self.is_postgres = bool(DATABASE_URL)
        if self.is_postgres:
            if psycopg2 is None:
                raise RuntimeError("DATABASE_URL is set but psycopg2-binary is not installed.")
            url = DATABASE_URL
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://"):]
            self.conn = psycopg2.connect(
                url,
                sslmode=os.environ.get("PGSSLMODE", "require"),
                cursor_factory=DictCursor,
            )
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def _sql(self, sql):
        if not self.is_postgres:
            return sql
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
        sql = sql.replace("?", "%s")
        if re.search(r"INSERT\s+INTO", sql, flags=re.I) and "ON CONFLICT" not in sql.upper() and re.search(r"INSERT\s+INTO\s+\w+\s*\([^)]*\)\s*VALUES", sql, flags=re.I):
            sql += " ON CONFLICT DO NOTHING"
        return sql

    def execute(self, sql, params=()):
        if self.is_postgres:
            cur = self.conn.cursor()
            cur.execute(self._sql(sql), params)
            return cur
        return self.conn.execute(sql, params)

    def executescript(self, sql):
        if self.is_postgres:
            cur = self.conn.cursor()

            statements = [
                statement.strip()
                for statement in sql.split(";")
                if statement.strip()
            ]

            for statement in statements:
                cur.execute(self._sql(statement))

            return cur

        return self.conn.executescript(sql)
        
    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def db():
    return DBConnection()


def init_db():
    conn = db()

    if conn.is_postgres:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'worker',
            activated INTEGER NOT NULL DEFAULT 0,
            activation_tx_ref TEXT,
            activation_transaction_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bank_accounts (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bank_code TEXT NOT NULL,
            bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            account_name TEXT,
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, account_number)
        );
        CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    task_link TEXT,
    reward INTEGER NOT NULL,
    deadline TEXT,
    slots INTEGER NOT NULL DEFAULT 1,
    difficulty TEXT NOT NULL DEFAULT 'Beginner',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
        CREATE TABLE IF NOT EXISTS submissions (
            id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, proof TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', reviewer_note TEXT, submitted_at TEXT NOT NULL, reviewed_at TEXT, proof_file TEXT
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, amount INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL, description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bank_account_id BIGINT NOT NULL REFERENCES bank_accounts(id), amount INTEGER NOT NULL, fee INTEGER NOT NULL DEFAULT 0,
            net_amount INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            provider_transfer_id TEXT, note TEXT, requested_at TEXT NOT NULL, processed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payment_events (
            id BIGSERIAL PRIMARY KEY, tx_ref TEXT UNIQUE NOT NULL, user_id BIGINT, event_type TEXT NOT NULL,
            transaction_id TEXT, amount INTEGER, currency TEXT, raw_json TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
        CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
        CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
                """)

        conn.execute(
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_link TEXT"
        )

    else:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker', activated INTEGER NOT NULL DEFAULT 0,
            activation_tx_ref TEXT, activation_transaction_id TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bank_code TEXT NOT NULL, bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL, account_name TEXT, is_verified INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            UNIQUE(user_id, account_number), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    task_link TEXT,
    reward INTEGER NOT NULL,
    deadline TEXT,
    slots INTEGER NOT NULL DEFAULT 1,
    difficulty TEXT NOT NULL DEFAULT 'Beginner',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, user_id INTEGER NOT NULL, proof TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', reviewer_note TEXT, submitted_at TEXT NOT NULL, reviewed_at TEXT, proof_file TEXT,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL,
            reference TEXT UNIQUE NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available', created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bank_account_id INTEGER NOT NULL,
            amount INTEGER NOT NULL, fee INTEGER NOT NULL DEFAULT 0, net_amount INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', provider_transfer_id TEXT, note TEXT, requested_at TEXT NOT NULL, processed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE, FOREIGN KEY(bank_account_id) REFERENCES bank_accounts(id)
        );
        CREATE TABLE IF NOT EXISTS payment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tx_ref TEXT UNIQUE NOT NULL, user_id INTEGER, event_type TEXT NOT NULL,
            transaction_id TEXT, amount INTEGER, currency TEXT, raw_json TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_submissions_user ON submissions(user_id);
        CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
        CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
        """)
            # Add proof_file column to existing submissions table
    if conn.is_postgres:
        conn.execute(
            "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS proof_file TEXT"
        )
    else:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(submissions)"
            ).fetchall()
        }

        if "proof_file" not in columns:
            conn.execute(
                "ALTER TABLE submissions ADD COLUMN proof_file TEXT"
            )
    admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO users(full_name,email,phone,password_hash,role,activated,created_at) VALUES(?,?,?,?,?,?,?)",
            ("TASKORA Admin", "admin@taskora.local", "0000000000",
             generate_password_hash(os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")), "admin", 1, now())
        )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def lagos_now():
    return datetime.now(LAGOS_TZ)


def is_friday():
    return lagos_now().weekday() == 4


def valid_amount(value):
    try:
        amount = int(str(value))
        return amount if amount > 0 else 0
    except (TypeError, ValueError):
        return 0


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or u["role"] != "admin":
            flash("Admin access required.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def available_balance(user_id):
    conn = db()
    credit = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=? AND kind='earning' AND status='available'",
        (user_id,)
    ).fetchone()[0]
    debit = conn.execute(
        "SELECT COALESCE(SUM(net_amount+fee),0) FROM withdrawals WHERE user_id=? AND status IN ('pending','processing','paid')",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return max(0, credit - debit)


def pending_balance(user_id):
    conn = db()
    val = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user_id=? AND kind='earning' AND status='pending'",
        (user_id,)
    ).fetchone()[0]
    conn.close()
    return val


def flw_headers():
    return {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def flw_post(path, payload):
    if not FLW_SECRET_KEY:
        raise RuntimeError("FLW_SECRET_KEY is not configured.")
    r = requests.post("https://api.flutterwave.com/v3" + path,
                      headers=flw_headers(), json=payload, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"status": "error", "message": r.text}
    if r.status_code >= 400 or data.get("status") == "error":
        raise RuntimeError(data.get("message", "Flutterwave request failed."))
    return data


def flw_get(path):
    if not FLW_SECRET_KEY:
        raise RuntimeError("FLW_SECRET_KEY is not configured.")
    r = requests.get("https://api.flutterwave.com/v3" + path, headers=flw_headers(), timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"status": "error", "message": r.text}
    if r.status_code >= 400 or data.get("status") == "error":
        raise RuntimeError(data.get("message", "Flutterwave request failed."))
    return data


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "activation_fee": ACTIVATION_FEE,
        "min_withdrawal": MIN_WITHDRAWAL,
    }


@app.route("/")
def index():
    conn = db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE status='open' ORDER BY id DESC LIMIT 6"
    ).fetchall()
    conn.close()
    return render_template("index.html", tasks=tasks)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        if len(full_name) < 2 or not email or len(phone) < 7 or len(password) < 8:
            flash("Fill all fields correctly. Password must be at least 8 characters.", "error")
            return render_template("register.html")
        conn = db()
        try:
            conn.execute(
                "INSERT INTO users(full_name,email,phone,password_hash,created_at) VALUES(?,?,?,?,?)",
                (full_name, email, phone, generate_password_hash(password), now())
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        except Exception:
            flash("Email or phone number is already registered.", "error")
            return render_template("register.html")
        finally:
            conn.close()
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(url_for("admin_dashboard") if user["role"] == "admin" else url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    conn = db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE status='open' ORDER BY id DESC LIMIT 8"
    ).fetchall()
    submissions = conn.execute("""
        SELECT s.*, t.title FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE s.user_id=? ORDER BY s.id DESC LIMIT 5
    """, (u["id"],)).fetchall()
    conn.close()
    return render_template(
        "dashboard.html",
        user=u,
        tasks=tasks,
        submissions=submissions,
        balance=available_balance(u["id"]),
        pending=pending_balance(u["id"])
    )


@app.route("/activate")
@login_required
def activate():
    u = current_user()
    if u["activated"]:
        return redirect(url_for("dashboard"))
    return render_template("activate.html")


@app.route("/activate/pay", methods=["POST"])
@login_required
def activate_pay():
    """Create a real Flutterwave checkout transaction for this TASKORA user."""
    u = current_user()

    if u["activated"]:
        return redirect(url_for("dashboard"))

    if not FLW_SECRET_KEY:
        flash("Flutterwave is not configured on the server.", "error")
        return redirect(url_for("activate"))

    tx_ref = f"TASKORA-ACT-{u['id']}-{uuid.uuid4().hex[:16]}"
    redirect_url = f"{BASE_URL}/activate/callback"

    payload = {
        "tx_ref": tx_ref,
        "amount": ACTIVATION_FEE,
        "currency": CURRENCY,
        "redirect_url": redirect_url,
        "payment_options": "card,banktransfer,ussd",
        "customer": {
            "email": u["email"],
            "name": u["full_name"],
            "phonenumber": u["phone"],
        },
        "customizations": {
            "title": "TASKORA WORK Activation",
            "description": "TASKORA WORK account activation",
        },
    }

    try:
        result = flw_post("/payments", payload)
        checkout_link = str((result.get("data") or {}).get("link") or "").strip()
        if not checkout_link:
            raise RuntimeError("Flutterwave did not return a checkout link.")
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("activate"))

    conn = db()
    try:
        conn.execute(
            "UPDATE users SET activation_tx_ref=? WHERE id=? AND activated=0",
            (tx_ref, u["id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO payment_events("
            "tx_ref,user_id,event_type,amount,currency,raw_json,created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                tx_ref,
                u["id"],
                "activation_started",
                ACTIVATION_FEE,
                CURRENCY,
                json.dumps({
                    "tx_ref": tx_ref,
                    "redirect_url": redirect_url,
                    "flutterwave_response": result,
                }),
                now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return redirect(checkout_link)


def _activation_started_for_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT tx_ref FROM payment_events "
        "WHERE user_id=? AND event_type='activation_started' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def _transaction_already_used(transaction_id):
    conn = db()
    row = conn.execute(
        "SELECT id FROM payment_events "
        "WHERE transaction_id=? AND event_type='activation_verified' LIMIT 1",
        (str(transaction_id),),
    ).fetchone()
    conn.close()
    return bool(row)


def verify_activation_transaction(transaction_id, user):
    """Verify the real Flutterwave transaction before activating the account."""
    if not FLW_SECRET_KEY:
        raise RuntimeError("FLW_SECRET_KEY is not configured.")

    transaction_id = str(transaction_id).strip()
    if not transaction_id:
        raise RuntimeError("Flutterwave transaction ID is missing.")

    r = requests.get(
        f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
        headers=flw_headers(),
        timeout=30,
    )

    try:
        data = r.json()
    except Exception:
        data = {"status": "error", "message": r.text}

    if r.status_code >= 400 or data.get("status") != "success":
        raise RuntimeError("Flutterwave could not verify this payment.")

    tx = data.get("data") or {}
    tx_status = str(tx.get("status") or "").lower()
    tx_currency = str(tx.get("currency") or "").upper()
    provider_tx_ref = str(tx.get("tx_ref") or "").strip()

    try:
        tx_amount = float(tx.get("amount", 0))
    except (TypeError, ValueError):
        tx_amount = 0

    paid_email = str(
        (tx.get("customer") or {}).get("email") or ""
    ).strip().lower()
    account_email = str(user["email"] or "").strip().lower()

    if tx_status != "successful":
        raise RuntimeError("Payment is not successful.")
    if tx_amount != float(ACTIVATION_FEE):
        raise RuntimeError(
            f"Invalid activation amount. Expected ₦{ACTIVATION_FEE:,}."
        )
    if tx_currency != CURRENCY:
        raise RuntimeError("Invalid payment currency.")
    if not paid_email or paid_email != account_email:
        raise RuntimeError(
            "The payment email does not match your TASKORA account email."
        )

    started = _activation_started_for_user(user["id"])
    if not started:
        raise RuntimeError(
            "No pending TASKORA activation payment was found for this account."
        )

    if provider_tx_ref != str(started["tx_ref"]):
        raise RuntimeError(
            "This Flutterwave transaction does not match the TASKORA activation request."
        )

    if _transaction_already_used(transaction_id):
        raise RuntimeError("This Flutterwave transaction has already been used.")

    return data, tx, str(started["tx_ref"])


@app.route("/activate/callback")
@login_required
def activation_callback():
    """Verify Flutterwave payment server-side before activating the worker."""
    status = str(request.args.get("status") or "").lower()
    transaction_id = (
        request.args.get("transaction_id")
        or request.args.get("transactionId")
    )

    u = current_user()

    if u["activated"]:
        return redirect(url_for("dashboard"))

    if status != "successful" or not transaction_id:
        flash(
            "Payment was not completed or transaction ID is missing.",
            "error",
        )
        return redirect(url_for("activate"))

    try:
        verified_data, tx, local_tx_ref = verify_activation_transaction(
            transaction_id, u
        )

        provider_tx_ref = str(tx.get("tx_ref") or "").strip()
        event_ref = f"FLW-ACT-{transaction_id}"

        conn = db()
        try:
            already_verified = conn.execute(
                "SELECT id FROM payment_events "
                "WHERE transaction_id=? AND event_type='activation_verified' LIMIT 1",
                (str(transaction_id),),
            ).fetchone()

            if already_verified:
                flash("This payment has already been verified.", "error")
                return redirect(url_for("activate"))

            updated = conn.execute(
                "UPDATE users SET activated=1, activation_transaction_id=? "
                "WHERE id=? AND activated=0",
                (str(transaction_id), u["id"]),
            )

            if updated.rowcount != 1:
                conn.rollback()
                flash("Account activation could not be completed.", "error")
                return redirect(url_for("activate"))

            conn.execute(
                "INSERT OR IGNORE INTO payment_events("
                "tx_ref,user_id,event_type,transaction_id,amount,currency,"
                "raw_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_ref,
                    u["id"],
                    "activation_verified",
                    str(transaction_id),
                    ACTIVATION_FEE,
                    CURRENCY,
                    json.dumps({
                        "local_activation_ref": local_tx_ref,
                        "provider_tx_ref": provider_tx_ref,
                        "flutterwave_response": verified_data,
                    }),
                    now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        flash(
            "Payment confirmed. Your TASKORA account is now activated.",
            "success",
        )
        return redirect(url_for("dashboard"))

    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("activate"))


@app.route("/webhooks/flutterwave", methods=["POST"])
def flutterwave_webhook():
    """Receive Flutterwave events and independently verify successful payments."""
    if FLW_WEBHOOK_HASH:
        supplied = request.headers.get("verif-hash", "")
        if not supplied or not secrets.compare_digest(
            supplied, FLW_WEBHOOK_HASH
        ):
            return jsonify({"ok": False}), 401

    payload = request.get_json(silent=True) or {}
    event = str(
        payload.get("event") or payload.get("event_type") or "unknown"
    ).lower()
    data = payload.get("data") or {}
    transaction_id = data.get("id")

    if not transaction_id:
        return jsonify({
            "received": True,
            "verified": False,
            "reason": "missing_transaction_id",
        }), 200

    transaction_id = str(transaction_id).strip()

    try:
        verified = flw_get(
            f"/transactions/{transaction_id}/verify"
        )
        tx = verified.get("data") or {}

        status = str(tx.get("status") or "").lower()
        currency = str(tx.get("currency") or "").upper()

        try:
            amount = float(tx.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0

        email = str(
            (tx.get("customer") or {}).get("email") or ""
        ).strip().lower()
        provider_tx_ref = str(tx.get("tx_ref") or "").strip()

        audit_ref = (
            provider_tx_ref
            if provider_tx_ref
            else f"FLW-EVENT-{transaction_id}-{event}"
        )

        conn = db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO payment_events("
                "tx_ref,event_type,transaction_id,amount,currency,raw_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    audit_ref,
                    event,
                    transaction_id,
                    data.get("amount"),
                    data.get("currency"),
                    json.dumps(payload),
                    now(),
                ),
            )

            if (
                event in {
                    "charge.completed",
                    "payment.completed",
                    "charge.completed.v2",
                }
                and status == "successful"
                and amount == float(ACTIVATION_FEE)
                and currency == CURRENCY
                and email
                and provider_tx_ref
            ):
                user = conn.execute(
                    "SELECT id, activation_tx_ref, activated "
                    "FROM users WHERE LOWER(email)=? AND role='worker' LIMIT 1",
                    (email,),
                ).fetchone()

                if (
                    user
                    and not user["activated"]
                    and str(user["activation_tx_ref"] or "") == provider_tx_ref
                ):
                    already_used = conn.execute(
                        "SELECT id FROM payment_events "
                        "WHERE transaction_id=? AND event_type='activation_verified' LIMIT 1",
                        (transaction_id,),
                    ).fetchone()

                    if not already_used:
                        conn.execute(
                            "UPDATE users SET activated=1, activation_transaction_id=? "
                            "WHERE id=? AND activated=0",
                            (transaction_id, user["id"]),
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO payment_events("
                            "tx_ref,user_id,event_type,transaction_id,amount,currency,"
                            "raw_json,created_at"
                            ") VALUES(?,?,?,?,?,?,?,?)",
                            (
                                f"FLW-ACT-{transaction_id}",
                                user["id"],
                                "activation_verified",
                                transaction_id,
                                ACTIVATION_FEE,
                                CURRENCY,
                                json.dumps({
                                    "provider_tx_ref": provider_tx_ref,
                                    "flutterwave_response": verified,
                                }),
                                now(),
                            ),
                        )

            conn.commit()
        finally:
            conn.close()

        return jsonify({"received": True, "verified": True}), 200

    except Exception:
        # Webhook delivery should be acknowledged; the redirect/verification
        # path remains the authoritative activation path.
        return jsonify({"received": True, "verified": False}), 200




@app.route("/tasks")
@login_required
def tasks():
    u = current_user()
    conn = db()
    rows = conn.execute("SELECT * FROM tasks WHERE status='open' ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("tasks.html", tasks=rows, user=u)


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    u = current_user()
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    already = conn.execute(
        "SELECT id FROM submissions WHERE task_id=? AND user_id=? AND status IN ('pending','approved')",
        (task_id, u["id"])
    ).fetchone()
    conn.close()
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("tasks"))
    return render_template("task_detail.html", task=task, already=already)
@app.route("/tasks/<int:task_id>/submit", methods=["POST"])
@login_required
def submit_task(task_id):
    u = current_user()

    if not u["activated"]:
        return redirect(url_for("activate"))

    proof = request.form.get("proof", "").strip()
    proof_file = request.files.get("proof_file")

    if not proof_file or not proof_file.filename:
        flash("Please upload a screenshot showing that you completed the task.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    if len(proof) < 5:
        flash("Please provide task proof/details.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    conn = db()

    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND status='open'",
        (task_id,)
    ).fetchone()

    existing = conn.execute(
        """
        SELECT id
        FROM submissions
        WHERE task_id=? AND user_id=?
        AND status IN ('pending','approved')
        """,
        (task_id, u["id"])
    ).fetchone()

    active_slots = conn.execute(
        """
        SELECT COUNT(*)
        FROM submissions
        WHERE task_id=?
        AND status IN ('pending','approved')
        """,
        (task_id,)
    ).fetchone()[0] if task else 0

    deadline_passed = False

    if task and task["deadline"]:
        try:
            deadline_passed = datetime.fromisoformat(
                task["deadline"]
            ).replace(tzinfo=LAGOS_TZ) < lagos_now()
        except ValueError:
            deadline_passed = False

    if not task or existing or active_slots >= task["slots"] or deadline_passed:
        conn.close()
        flash(
            "Task unavailable, full, expired, or already submitted.",
            "error"
        )
        return redirect(url_for("tasks"))

    # Read uploaded screenshot
    file_bytes = proof_file.read()

    if not file_bytes:
        conn.close()
        flash("The uploaded screenshot is empty.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    # Store image as base64 in database
    import base64

    proof_file_data = base64.b64encode(file_bytes).decode("utf-8")

    content_type = proof_file.mimetype or "image/jpeg"

    proof_file_data = f"data:{content_type};base64,{proof_file_data}"

    # Create the submission first so we have a stable ID for its earning record.
    # The earning is intentionally PENDING until an admin approves the proof.
    conn.execute(
        """
        INSERT INTO submissions
        (task_id, user_id, proof, proof_file, submitted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, u["id"], proof, proof_file_data, now())
    )

    submission = conn.execute(
        """
        SELECT id
        FROM submissions
        WHERE task_id=? AND user_id=? AND status='pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (task_id, u["id"])
    ).fetchone()

    if not submission:
        conn.rollback()
        conn.close()
        flash("Could not create the task submission. Please try again.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    submission_id = submission["id"]
    earning_ref = f"TASKORA-EARN-{u['id']}-{submission_id}"

    conn.execute(
        """
        INSERT OR IGNORE INTO ledger
        (user_id, kind, amount, reference, description, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            u["id"],
            "earning",
            task["reward"],
            earning_ref,
            f"Pending task: {task['title']}",
            "pending",
            now(),
        )
    )

    conn.commit()
    conn.close()

    flash("Task submitted for review. Your reward is pending admin approval.", "success")
    return redirect(url_for("dashboard"))


@app.route("/wallet")
@login_required
def wallet():
    u = current_user()
    conn = db()
    ledger = conn.execute(
        "SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],)
    ).fetchall()
    withdrawals = conn.execute(
        "SELECT w.*, b.bank_name,b.account_number FROM withdrawals w JOIN bank_accounts b ON b.id=w.bank_account_id WHERE w.user_id=? ORDER BY w.id DESC LIMIT 30",
        (u["id"],)
    ).fetchall()
    banks = conn.execute("SELECT * FROM bank_accounts WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    conn.close()
    return render_template("wallet.html", balance=available_balance(u["id"]), pending=pending_balance(u["id"]),
                           ledger=ledger, withdrawals=withdrawals, banks=banks)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Allow a logged-in worker to change their password securely."""
    if request.method == "GET":
        # The profile page already contains the password form/link.
        # Redirecting here also keeps the feature compatible with the
        # existing profile.html without requiring another template file.
        return redirect(url_for("profile"))

    u = current_user()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password or not confirm_password:
        flash("Please fill in all password fields.", "error")
        return redirect(url_for("profile"))

    if not check_password_hash(u["password_hash"], current_password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("profile"))

    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "error")
        return redirect(url_for("profile"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "error")
        return redirect(url_for("profile"))

    if check_password_hash(u["password_hash"], new_password):
        flash("Your new password must be different from your current password.", "error")
        return redirect(url_for("profile"))

    conn = db()
    try:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), u["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        flash("Could not change your password. Please try again.", "error")
        return redirect(url_for("profile"))
    finally:
        conn.close()

    flash("Password changed successfully.", "success")
    return redirect(url_for("profile"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u = current_user()
    conn = db()
    if request.method == "POST":
        bank_code = request.form.get("bank_code", "").strip()
        bank_name = request.form.get("bank_name", "").strip()
        account_number = request.form.get("account_number", "").strip()
        account_name = request.form.get("account_name", "").strip()
        if not (bank_code and bank_name and account_number.isdigit() and len(account_number) == 10):
            flash("Enter a valid Nigerian 10-digit account number and bank details.", "error")
        else:
            try:
                conn.execute(
                    "INSERT INTO bank_accounts(user_id,bank_code,bank_name,account_number,account_name,created_at) VALUES(?,?,?,?,?,?)",
                    (u["id"], bank_code, bank_name, account_number, account_name, now())
                )
                conn.commit()
                flash("Bank account saved. Verify the account name before withdrawing.", "success")
            except Exception:
                flash("That bank account is already saved.", "error")
    banks = conn.execute("SELECT * FROM bank_accounts WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    conn.close()
    return render_template("profile.html", user=u, banks=banks)


@app.route("/withdraw", methods=["POST"])
@login_required
def withdraw():
    u = current_user()
    if not u["activated"]:
        return redirect(url_for("activate"))
    amount = int(request.form.get("amount", "0") or 0)
    bank_id = int(request.form.get("bank_id", "0") or 0)
    if amount < MIN_WITHDRAWAL:
        flash(f"Minimum withdrawal is ₦{MIN_WITHDRAWAL:,}.", "error")
        return redirect(url_for("wallet"))

    # Friday-only request window. Admin can process at any time after review.
    if not is_friday():
        flash("Weekly withdrawal requests open on Friday. Your balance remains safe in your wallet.", "error")
        return redirect(url_for("wallet"))

    conn = db()
    bank = conn.execute("SELECT * FROM bank_accounts WHERE id=? AND user_id=?", (bank_id, u["id"])).fetchone()
    conn.close()
    if not bank:
        flash("Select a valid bank account first.", "error")
        return redirect(url_for("wallet"))
    if not bank["is_verified"]:
        flash("Your bank account must be verified before withdrawal.", "error")
        return redirect(url_for("wallet"))

    # Prevent duplicate requests while another payout is still being processed.
    conn = db()
    existing_wd = conn.execute(
        "SELECT id FROM withdrawals WHERE user_id=? AND status IN ('pending','processing') LIMIT 1",
        (u["id"],)
    ).fetchone()
    conn.close()
    if existing_wd:
        flash("You already have a withdrawal being processed.", "error")
        return redirect(url_for("wallet"))

    balance = available_balance(u["id"])
    if amount > balance:
        flash("Insufficient available balance.", "error")
        return redirect(url_for("wallet"))

    # Example transparent platform fee; can be changed in settings later.
    fee = 50 if amount >= 10000 else 25
    net = amount - fee
    if net <= 0:
        flash("Withdrawal amount is too small after fees.", "error")
        return redirect(url_for("wallet"))

    ref = f"TASKORA-WD-{u['id']}-{uuid.uuid4().hex[:12]}"
    conn = db()
    conn.execute(
        "INSERT INTO withdrawals(user_id,bank_account_id,amount,fee,net_amount,reference,requested_at) VALUES(?,?,?,?,?,?,?)",
        (u["id"], bank["id"], amount, fee, net, ref, now())
    )
    conn.commit()
    conn.close()
    flash("Withdrawal request submitted for weekly processing.", "success")
    return redirect(url_for("wallet"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = db()
    stats = {
        "users": conn.execute("SELECT COUNT(*) FROM users WHERE role='worker'").fetchone()[0],
        "activated": conn.execute("SELECT COUNT(*) FROM users WHERE role='worker' AND activated=1").fetchone()[0],
        "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "submissions": conn.execute("SELECT COUNT(*) FROM submissions WHERE status='pending'").fetchone()[0],
        "withdrawals": conn.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0],
        "activation_revenue": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payment_events WHERE event_type='activation_verified'"
        ).fetchone()[0],
        "paid_withdrawals": conn.execute("SELECT COUNT(*) FROM withdrawals WHERE status='paid'").fetchone()[0],
        "total_earnings": conn.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='earning' AND status='available'").fetchone()[0],
    }
    recent_withdrawals = conn.execute("""
        SELECT w.*, u.full_name, u.email, b.bank_name,b.account_number
        FROM withdrawals w
        JOIN users u ON u.id=w.user_id
        JOIN bank_accounts b ON b.id=w.bank_account_id
        ORDER BY w.id DESC LIMIT 20
    """).fetchall()
    conn.close()
    return render_template("admin.html", stats=stats, withdrawals=recent_withdrawals)


@app.route("/admin/tasks/new", methods=["GET", "POST"])
@admin_required
def admin_new_task():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        task_link = request.form.get("task_link", "").strip()
        description = request.form.get("description", "").strip()

        try:
            reward = int(request.form.get("reward", "0") or 0)
        except (TypeError, ValueError):
            reward = 0

        try:
            slots = int(request.form.get("slots", "1") or 1)
        except (TypeError, ValueError):
            slots = 1

        deadline = request.form.get("deadline", "").strip()
        difficulty = request.form.get("difficulty", "Beginner").strip()

        # Basic validation
        if not title:
            flash("Task title is required.", "error")
            return render_template("admin_task.html")

        if not category:
            flash("Please select a platform/category.", "error")
            return render_template("admin_task.html")

        if not task_link:
            flash("Task link is required.", "error")
            return render_template("admin_task.html")

        if not task_link.startswith(("http://", "https://")):
            flash("Task link must start with http:// or https://.", "error")
            return render_template("admin_task.html")

        if not description:
            flash("Task description is required.", "error")
            return render_template("admin_task.html")

        if reward <= 0:
            flash("Reward must be greater than zero.", "error")
            return render_template("admin_task.html")

        if slots <= 0:
            flash("Slots must be greater than zero.", "error")
            return render_template("admin_task.html")

        conn = db()

        try:
            conn.execute(
                """
                INSERT INTO tasks
                (
                    title,
                    category,
                    description,
                    task_link,
                    reward,
                    deadline,
                    slots,
                    difficulty,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    category,
                    description,
                    task_link,
                    reward,
                    deadline,
                    slots,
                    difficulty,
                    now(),
                )
            )

            conn.commit()

        except Exception as e:
            conn.rollback()
            flash(f"Could not create task: {str(e)}", "error")
            return render_template("admin_task.html")

        finally:
            conn.close()

        flash("Task created successfully.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_task.html")


@app.route("/admin/submissions")
@admin_required
def admin_submissions():
    conn = db()
    rows = conn.execute("""
        SELECT s.*, t.title, t.reward, u.full_name,u.email
        FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN users u ON u.id=s.user_id
        ORDER BY CASE WHEN s.status='pending' THEN 0 ELSE 1 END, s.id DESC
    """).fetchall()
    conn.close()
    return render_template("admin_submissions.html", submissions=rows)


@app.route("/admin/submissions/<int:submission_id>/<action>", methods=["POST"])
@admin_required
def review_submission(submission_id, action):
    if action not in ("approve","reject"):
        return redirect(url_for("admin_submissions"))
    conn = db()
    s = conn.execute("""
        SELECT s.*, t.reward, t.title FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE s.id=?
    """, (submission_id,)).fetchone()
    if not s or s["status"] != "pending":
        conn.close()
        flash("Submission is no longer pending.", "error")
        return redirect(url_for("admin_submissions"))

    ref = f"TASKORA-EARN-{s['user_id']}-{s['id']}"

    if action == "approve":
        # Move the existing pending earning to AVAILABLE.
        # If this is an older submission created before the pending-ledger fix,
        # create the available earning once as a safe backward-compatible fallback.
        conn.execute(
            "UPDATE submissions SET status='approved',reviewed_at=? WHERE id=? AND status='pending'",
            (now(), submission_id)
        )

        updated = conn.execute(
            "UPDATE ledger SET status='available', description=? "
            "WHERE reference=? AND user_id=? AND kind='earning' AND status='pending'",
            (f"Approved task: {s['title']}", ref, s["user_id"])
        )

        if updated.rowcount == 0:
            conn.execute(
                "INSERT OR IGNORE INTO ledger("
                "user_id,kind,amount,reference,description,status,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    s["user_id"],
                    "earning",
                    s["reward"],
                    ref,
                    f"Approved task: {s['title']}",
                    "available",
                    now(),
                )
            )

        flash("Submission approved and earnings credited.", "success")
    else:
        note = request.form.get("note", "Task submission rejected.").strip()[:500]
        conn.execute(
            "UPDATE submissions SET status='rejected',reviewer_note=?,reviewed_at=? WHERE id=? AND status='pending'",
            (note, now(), submission_id)
        )

        # Keep a rejected earning record for audit/history, but make sure it
        # cannot appear in Pending or Available balance.
        conn.execute(
            "UPDATE ledger SET status='rejected', description=? "
            "WHERE reference=? AND user_id=? AND kind='earning' AND status='pending'",
            (f"Rejected task: {s['title']}", ref, s["user_id"])
        )
        flash("Submission rejected.", "success")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_submissions"))


@app.route("/admin/withdrawals")
@admin_required
def admin_withdrawals():
    conn = db()
    rows = conn.execute("""
        SELECT w.*,u.full_name,u.email,b.bank_name,b.bank_code,b.account_number,b.account_name
        FROM withdrawals w JOIN users u ON u.id=w.user_id
        JOIN bank_accounts b ON b.id=w.bank_account_id
        ORDER BY CASE WHEN w.status='pending' THEN 0 ELSE 1 END,w.id DESC
    """).fetchall()
    conn.close()
    return render_template("admin_withdrawals.html", withdrawals=rows)


@app.route("/admin/withdrawals/<int:withdrawal_id>/pay", methods=["POST"])
@admin_required
def admin_pay_withdrawal(withdrawal_id):
    conn = db()
    w = conn.execute("""
        SELECT w.*,u.full_name,b.bank_code,b.account_number,b.account_name
        FROM withdrawals w JOIN users u ON u.id=w.user_id
        JOIN bank_accounts b ON b.id=w.bank_account_id WHERE w.id=?
    """, (withdrawal_id,)).fetchone()
    conn.close()
    if not w or w["status"] != "pending":
        flash("Withdrawal is not pending.", "error")
        return redirect(url_for("admin_withdrawals"))

    payload = {
        "account_bank": w["bank_code"],
        "account_number": w["account_number"],
        "amount": w["net_amount"],
        "currency": CURRENCY,
        "narration": "TASKORA WORK weekly earnings",
        "reference": w["reference"],
        "beneficiary_name": w["account_name"] or w["full_name"],
        "debit_currency": CURRENCY
    }
    try:
        result = flw_post("/transfers", payload)
        transfer_id = str((result.get("data") or {}).get("id") or "")
        conn = db()
        conn.execute(
            "UPDATE withdrawals SET status='processing',provider_transfer_id=?,processed_at=?,note=? WHERE id=?",
            (transfer_id, now(), "Transfer submitted to Flutterwave.", withdrawal_id)
        )
        conn.commit()
        conn.close()
        flash("Transfer submitted to Flutterwave. Confirm final status via provider/webhook.", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("admin_withdrawals"))



@app.route("/admin/users")
@admin_required
def admin_users():
    conn = db()
    users = conn.execute("SELECT * FROM users WHERE role='worker' ORDER BY id DESC").fetchall()
    banks = conn.execute("SELECT * FROM bank_accounts ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_users.html", users=users, banks=banks)


@app.route("/admin/banks/<int:bank_id>/verify", methods=["POST"])
@admin_required
def admin_verify_bank(bank_id):
    conn = db()
    bank = conn.execute("SELECT id FROM bank_accounts WHERE id=?", (bank_id,)).fetchone()
    if not bank:
        conn.close()
        flash("Bank account not found.", "error")
        return redirect(url_for("admin_users"))
    conn.execute("UPDATE bank_accounts SET is_verified=1 WHERE id=?", (bank_id,))
    conn.commit()
    conn.close()
    flash("Bank account verified.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle-activation", methods=["POST"])
@admin_required
def admin_toggle_activation(user_id):
    conn = db()
    user = conn.execute("SELECT activated FROM users WHERE id=? AND role='worker'", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash("Worker not found.", "error")
        return redirect(url_for("admin_users"))
    new_value = 0 if user["activated"] else 1
    conn.execute("UPDATE users SET activated=? WHERE id=?", (new_value, user_id))
    conn.commit()
    conn.close()
    flash("Worker activation status updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/withdrawals/<int:withdrawal_id>/reject", methods=["POST"])
@admin_required
def admin_reject_withdrawal(withdrawal_id):
    conn = db()
    w = conn.execute("SELECT status FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
    if not w or w["status"] != "pending":
        conn.close()
        flash("Withdrawal is not pending.", "error")
        return redirect(url_for("admin_withdrawals"))
    note = request.form.get("note", "Withdrawal rejected by admin.").strip()[:500]
    conn.execute("UPDATE withdrawals SET status='rejected', note=?, processed_at=? WHERE id=?", (note, now(), withdrawal_id))
    conn.commit()
    conn.close()
    flash("Withdrawal rejected. The amount remains available to the worker.", "success")
    return redirect(url_for("admin_withdrawals"))


@app.route("/admin/withdrawals/<int:withdrawal_id>/refresh", methods=["POST"])
@admin_required
def admin_refresh_withdrawal(withdrawal_id):
    conn = db()
    w = conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
    conn.close()
    if not w or not w["provider_transfer_id"]:
        flash("No provider transfer ID is available.", "error")
        return redirect(url_for("admin_withdrawals"))
    try:
        result = flw_get(f"/transfers/{w['provider_transfer_id']}")
        data = result.get("data") or {}
        status = str(data.get("status") or data.get("transfer_status") or "").lower()
        new_status = None
        if status in {"successful", "success", "completed"}:
            new_status = "paid"
        elif status in {"failed", "cancelled", "canceled", "reversed"}:
            new_status = "failed"
        if new_status:
            conn = db()
            conn.execute("UPDATE withdrawals SET status=?, note=?, processed_at=? WHERE id=?", (new_status, f"Provider status: {status}", now(), withdrawal_id))
            conn.commit()
            conn.close()
            flash(f"Withdrawal status updated to {new_status}.", "success")
        else:
            flash(f"Provider status is {status or 'unknown'}; no final status change made.", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("admin_withdrawals"))

# ===============================
# PUBLIC LEGAL / SUPPORT PAGES
# These endpoints are required by templates/base.html.
# ===============================

@app.route("/terms")
def terms():
    return """
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TASKORA WORK — Terms & Conditions</title>
    <style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.7}a{color:inherit}</style>
    </head><body>
    <h1>TASKORA WORK — Terms &amp; Conditions</h1>
    <p>By using TASKORA WORK, you agree to use the platform lawfully and to provide accurate account information.</p>
    <p>Tasks must be completed honestly and according to the instructions provided. Fraudulent activity, duplicate submissions, or attempts to abuse the platform may result in account restrictions.</p>
    <p>Activation, task rewards, withdrawals, and other platform rules are subject to the current rules displayed inside TASKORA WORK.</p>
    <p><a href="/">← Back to TASKORA WORK</a></p>
    </body></html>
    """


@app.route("/privacy")
def privacy():
    return """
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TASKORA WORK — Privacy Policy</title>
    <style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.7}a{color:inherit}</style>
    </head><body>
    <h1>TASKORA WORK — Privacy Policy</h1>
    <p>TASKORA WORK uses the information you provide to create and operate your account, process tasks, manage balances, and process withdrawals.</p>
    <p>We do not ask users to submit information that is not needed for the operation of the service. Payment-related information is handled for payment verification and account operations.</p>
    <p><a href="/">← Back to TASKORA WORK</a></p>
    </body></html>
    """


@app.route("/support")
def support():
    return """
    <!doctype html>
    <html lang="en">
    <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>TASKORA WORK — Support</title>
    <style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.7}a{color:inherit}</style>
    </head><body>
    <h1>TASKORA WORK — Support</h1>
    <p>If you need help with your account, activation, tasks, wallet, or withdrawal, use the support contact provided by TASKORA WORK.</p>
    <p>Please include your registered email and a clear description of the issue when contacting support.</p>
    <p><a href="/">← Back to TASKORA WORK</a></p>
    </body></html>
    """


@app.route("/health")
def health():
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
        backend = "postgresql" if conn.is_postgres else "sqlite"
        conn.close()
        return jsonify({"status": "ok", "service": "taskora-work", "database": backend})
    except Exception:
        return jsonify({"status": "error", "service": "taskora-work", "database": "unavailable"}), 503


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
