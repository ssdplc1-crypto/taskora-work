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

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "taskora.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


# Business / Advertiser configuration
ADVERTISER_PLATFORM_FEE_PERCENT = int(os.getenv("ADVERTISER_PLATFORM_FEE_PERCENT", "20"))
ADVERTISER_PAYMENT_PROVIDER = os.getenv("ADVERTISER_PAYMENT_PROVIDER", "flutterwave")
ADVERTISER_TASK_APPROVAL = os.getenv("ADVERTISER_TASK_APPROVAL", "admin")
ADVERTISER_SUBMISSION_APPROVAL = os.getenv("ADVERTISER_SUBMISSION_APPROVAL", "admin")
ADVERTISER_MIN_FUNDING = int(os.getenv("ADVERTISER_MIN_FUNDING", "100"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "CHANGE_THIS_IN_PRODUCTION")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"

FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "")
FLW_WEBHOOK_HASH = os.environ.get("FLW_WEBHOOK_HASH", "")
FLW_PAYMENT_OPTIONS = os.environ.get(
    "FLW_PAYMENT_OPTIONS",
    "card, banktransfer, ussd, account, internetbanking, nqr, enaira, opay",
)
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
ACTIVATION_FEE = int(os.environ.get("ACTIVATION_AMOUNT", "3000"))
MIN_WITHDRAWAL = int(os.environ.get("MINIMUM_WITHDRAWAL", "2000"))
REFERRAL_REWARD = int(os.environ.get("REFERRAL_REWARD", "500"))
REFERRAL_CODE_LENGTH = 8
CURRENCY = "NGN"
LAGOS_TZ = ZoneInfo("Africa/Lagos")
FLUTTERWAVE_PAYMENT_LINK = os.environ.get(
    "FLUTTERWAVE_PAYMENT_LINK",
    "https://flutterwave.com/pay/io7rwhtgumk4"
).strip()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"


def get_openai_client():
    """Return an OpenAI client using the server-side environment key only."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured on the server.")
    if OpenAI is None:
        raise RuntimeError("OpenAI SDK is not installed. Add openai to requirements.txt and redeploy.")
    return OpenAI(api_key=OPENAI_API_KEY)


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
            referral_code TEXT UNIQUE,
            referred_by_user_id BIGINT REFERENCES users(id),
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
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_note TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
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
        conn.execute("ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0")

    else:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'worker', activated INTEGER NOT NULL DEFAULT 0,
            activation_tx_ref TEXT, activation_transaction_id TEXT,
            referral_code TEXT UNIQUE, referred_by_user_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, bank_code TEXT NOT NULL, bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL, account_name TEXT, is_verified INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', rejection_note TEXT, is_deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
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
    # Backward-compatible task migrations. Older TASKORA databases may have
    # been created before task_link/status/difficulty/slots were added.
    # Without these migrations, /tasks/<id> and admin task pages can return 500.
    if conn.is_postgres:
        for statement in (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_link TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS slots INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS difficulty TEXT NOT NULL DEFAULT 'Beginner'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TEXT",
        ):
            conn.execute(statement)
    else:
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        task_migrations = {
            "task_link": "ALTER TABLE tasks ADD COLUMN task_link TEXT",
            "deadline": "ALTER TABLE tasks ADD COLUMN deadline TEXT",
            "slots": "ALTER TABLE tasks ADD COLUMN slots INTEGER NOT NULL DEFAULT 1",
            "difficulty": "ALTER TABLE tasks ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'Beginner'",
            "status": "ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'open'",
            "created_at": "ALTER TABLE tasks ADD COLUMN created_at TEXT",
        }
        for column, statement in task_migrations.items():
            if column not in task_columns:
                conn.execute(statement)

    # Advertiser ownership migration. Existing admin-created tasks remain unowned;
    # advertiser-created tasks are always tied to the advertiser user id.
    if conn.is_postgres:
        conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_user_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_user ON tasks(owner_user_id)")
    else:
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "owner_user_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_user_id INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_user ON tasks(owner_user_id)")

    conn.execute("UPDATE tasks SET status='open' WHERE status IS NULL OR status=''")
    conn.execute("UPDATE tasks SET difficulty='Beginner' WHERE difficulty IS NULL OR difficulty=''")
    conn.execute("UPDATE tasks SET slots=1 WHERE slots IS NULL OR slots < 1")

    # Referral migrations: preserve existing users and give each a stable code.
    if conn.is_postgres:
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by_user_id BIGINT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)")
    else:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "referral_code" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        if "referred_by_user_id" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)")

    def _new_referral_code():
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(REFERRAL_CODE_LENGTH))
            if not conn.execute("SELECT 1 FROM users WHERE referral_code=?", (code,)).fetchone():
                return code

    existing_users = conn.execute("SELECT id FROM users WHERE referral_code IS NULL OR referral_code=''").fetchall()
    for row in existing_users:
        conn.execute("UPDATE users SET referral_code=? WHERE id=?", (_new_referral_code(), row["id"]))

    # Backward-compatible migrations for existing databases.
    # These are intentionally idempotent so Render/PostgreSQL and local SQLite
    # databases can both be upgraded without losing existing users or money.
    if conn.is_postgres:
        for statement in (
            "ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS is_deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE bank_accounts ADD COLUMN IF NOT EXISTS rejection_note TEXT",
        ):
            conn.execute(statement)
    else:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bank_accounts)").fetchall()}
        if "is_deleted" not in columns:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
        if "status" not in columns:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if "rejection_note" not in columns:
            conn.execute("ALTER TABLE bank_accounts ADD COLUMN rejection_note TEXT")

    # Keep old verified accounts consistent with the new status field.
    conn.execute("UPDATE bank_accounts SET status='verified' WHERE is_verified=1 AND (status IS NULL OR status='pending')")
    conn.execute("UPDATE bank_accounts SET status='pending' WHERE status IS NULL OR status=''" )

    # Advertiser campaign funding / escrow-style accounting. Money paid by a
    # business is credited to its internal campaign wallet, then reserved for
    # a specific task. Worker rewards and the proportional platform fee are
    # only settled when Admin approves a completed submission. Unused reserved
    # funds are released back to the advertiser wallet when a campaign closes.
    if conn.is_postgres:
        for statement in (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'unfunded'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS worker_budget BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS platform_fee BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS total_budget BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reserved_budget BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_slots INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS funded_at TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS approved_at TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS closed_at TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS refund_reference TEXT",
        ):
            conn.execute(statement)
    else:
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        task_migrations = {
            "payment_status": "ALTER TABLE tasks ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'unfunded'",
            "worker_budget": "ALTER TABLE tasks ADD COLUMN worker_budget INTEGER NOT NULL DEFAULT 0",
            "platform_fee": "ALTER TABLE tasks ADD COLUMN platform_fee INTEGER NOT NULL DEFAULT 0",
            "total_budget": "ALTER TABLE tasks ADD COLUMN total_budget INTEGER NOT NULL DEFAULT 0",
            "reserved_budget": "ALTER TABLE tasks ADD COLUMN reserved_budget INTEGER NOT NULL DEFAULT 0",
            "completed_slots": "ALTER TABLE tasks ADD COLUMN completed_slots INTEGER NOT NULL DEFAULT 0",
            "funded_at": "ALTER TABLE tasks ADD COLUMN funded_at TEXT",
            "approved_at": "ALTER TABLE tasks ADD COLUMN approved_at TEXT",
            "closed_at": "ALTER TABLE tasks ADD COLUMN closed_at TEXT",
            "refund_reference": "ALTER TABLE tasks ADD COLUMN refund_reference TEXT",
        }
        for column, statement in task_migrations.items():
            if column not in task_columns:
                conn.execute(statement)

    if conn.is_postgres:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertiser_wallets (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance BIGINT NOT NULL DEFAULT 0,
            reserved_balance BIGINT NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS advertiser_transactions (
            id BIGSERIAL PRIMARY KEY, advertiser_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            task_id BIGINT REFERENCES tasks(id) ON DELETE SET NULL, type TEXT NOT NULL, amount BIGINT NOT NULL,
            reference TEXT UNIQUE NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'completed', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS platform_revenue (
            id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            advertiser_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, submission_id BIGINT REFERENCES submissions(id) ON DELETE SET NULL,
            amount BIGINT NOT NULL, reference TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_adv_transactions_advertiser ON advertiser_transactions(advertiser_id);
        CREATE INDEX IF NOT EXISTS idx_adv_transactions_task ON advertiser_transactions(task_id);
        CREATE INDEX IF NOT EXISTS idx_platform_revenue_task ON platform_revenue(task_id);
        """)
    else:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS advertiser_wallets (
            user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, reserved_balance INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS advertiser_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, advertiser_id INTEGER NOT NULL, task_id INTEGER, type TEXT NOT NULL,
            amount INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL, FOREIGN KEY(advertiser_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS platform_revenue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, advertiser_id INTEGER NOT NULL, submission_id INTEGER,
            amount INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE, FOREIGN KEY(advertiser_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_adv_transactions_advertiser ON advertiser_transactions(advertiser_id);
        CREATE INDEX IF NOT EXISTS idx_adv_transactions_task ON advertiser_transactions(task_id);
        """)

    # Backfill advertiser accounting fields for old advertiser tasks. Existing
    # campaigns are left untouched financially; new campaigns use the full
    # funded/reserved flow.
    # Old advertiser campaigns created before the funding system are hidden from
    # workers until their owner funds them again. No money is fabricated during migration.
    conn.execute("UPDATE tasks SET status='awaiting_payment', payment_status='unfunded' WHERE owner_user_id IS NOT NULL AND payment_status='unfunded' AND status='open'")
    conn.execute("UPDATE tasks SET worker_budget=reward*slots WHERE owner_user_id IS NOT NULL AND worker_budget=0")
    conn.execute("UPDATE tasks SET platform_fee=(worker_budget * ?) / 100 WHERE owner_user_id IS NOT NULL AND platform_fee=0", (ADVERTISER_PLATFORM_FEE_PERCENT,))
    conn.execute("UPDATE tasks SET total_budget=worker_budget+platform_fee WHERE owner_user_id IS NOT NULL AND total_budget=0")
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
        "referral_reward": REFERRAL_REWARD,
    }


@app.route("/")
def index():
    conn = db()
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE status='open' ORDER BY id DESC LIMIT 6"
    ).fetchall()
    conn.close()
    return render_template("index.html", tasks=tasks)


@app.route("/r/<referral_code>")
def referral_link(referral_code):
    code = referral_code.strip().upper()
    conn = db()
    referrer = conn.execute(
        "SELECT id FROM users WHERE referral_code=? AND role='worker' LIMIT 1", (code,)
    ).fetchone()
    conn.close()
    if referrer:
        session["pending_referral_code"] = code
        return redirect(url_for("register"))
    flash("Referral code not found.", "error")
    return redirect(url_for("register"))


@app.route("/account-type")
def account_type():
    """Let a new user choose a dedicated Worker or Business account."""
    return render_template("account_type.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        referral_code = request.form.get("referral_code", "").strip().upper() or session.get("pending_referral_code", "")
        if len(full_name) < 2 or not email or len(phone) < 7 or len(password) < 8:
            flash("Fill all fields correctly. Password must be at least 8 characters.", "error")
            return render_template("register.html")
        conn = db()
        try:
            referrer = None
            if referral_code:
                referrer = conn.execute(
                    "SELECT id FROM users WHERE referral_code=? AND role='worker' LIMIT 1",
                    (referral_code,),
                ).fetchone()
                if not referrer:
                    flash("Invalid referral code. You can leave it blank or enter a valid code.", "error")
                    return render_template("register.html")

            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            code = None
            while not code:
                candidate = "".join(secrets.choice(alphabet) for _ in range(REFERRAL_CODE_LENGTH))
                if not conn.execute("SELECT 1 FROM users WHERE referral_code=?", (candidate,)).fetchone():
                    code = candidate

            conn.execute(
                "INSERT INTO users(full_name,email,phone,password_hash,referral_code,referred_by_user_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (full_name, email, phone, generate_password_hash(password), code,
                 referrer["id"] if referrer else None, now())
            )
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            session["user_id"] = user["id"]
            session.pop("pending_referral_code", None)
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
            role = str(user["role"] or "").lower()
            if role == "admin":
                return redirect(url_for("admin_dashboard"))
            if role in ("advertiser", "business"):
                return redirect(url_for("business_dashboard"))
            return redirect(url_for("dashboard"))
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
    all_open_tasks = conn.execute(
        """
        SELECT
            t.*,
            (t.slots - (
                SELECT COUNT(*)
                FROM submissions s
                WHERE s.task_id=t.id
                  AND s.status IN ('pending','approved')
            )) AS remaining_slots
        FROM tasks t
        WHERE t.status='open'
        ORDER BY t.id DESC
        """
    ).fetchall()
    tasks = [t for t in all_open_tasks if int(t["remaining_slots"] or 0) > 0][:8]
    available_task_count = len([t for t in all_open_tasks if int(t["remaining_slots"] or 0) > 0])
    open_slots = sum(max(0, int(t["remaining_slots"] or 0)) for t in all_open_tasks)
    highest_reward = max((int(t["reward"] or 0) for t in all_open_tasks if int(t["remaining_slots"] or 0) > 0), default=0)
    task_value = sum(
        int(t["reward"] or 0) * max(0, int(t["remaining_slots"] or 0))
        for t in all_open_tasks
    )
    submissions = conn.execute("""
        SELECT s.*, t.title FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE s.user_id=? ORDER BY s.id DESC LIMIT 5
    """, (u["id"],)).fetchall()
    referrals = conn.execute("""
        SELECT id, full_name, email, activated, created_at
        FROM users
        WHERE referred_by_user_id=? AND role='worker'
        ORDER BY id DESC
    """, (u["id"],)).fetchall()
    referral_approved = sum(1 for r in referrals if r["activated"])
    referral_pending = len(referrals) - referral_approved
    conn.close()
    return render_template(
        "dashboard.html",
        user=u,
        tasks=tasks,
        submissions=submissions,
        referrals=referrals,
        referral_approved=referral_approved,
        referral_pending=referral_pending,
        balance=available_balance(u["id"]),
        pending=pending_balance(u["id"]),
        available_task_count=available_task_count,
        open_slots=open_slots,
        highest_reward=highest_reward,
        task_value=task_value,
    )


def reward_referrer_for_activation(conn, activated_user_id):
    row = conn.execute(
        "SELECT referred_by_user_id FROM users WHERE id=? AND role='worker' AND activated=1",
        (activated_user_id,),
    ).fetchone()
    if not row or not row["referred_by_user_id"] or row["referred_by_user_id"] == activated_user_id:
        return False
    reference = f"TASKORA-REF-{activated_user_id}"
    existing = conn.execute("SELECT id FROM ledger WHERE reference=? LIMIT 1", (reference,)).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO ledger(user_id,kind,amount,reference,description,status,created_at) VALUES(?,?,?,?,?,?,?)",
        (row["referred_by_user_id"], "earning", REFERRAL_REWARD, reference,
         f"Referral reward: worker #{activated_user_id} activated", "available", now()),
    )
    return True


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
        "payment_options": "card",
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
            reward_referrer_for_activation(conn, u["id"])
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
                        reward_referrer_for_activation(conn, user["id"])

            # Advertiser campaign/wallet funding webhooks are also verified
            # server-side. The unique transaction event keeps callbacks idempotent.
            if (
                event in {"charge.completed", "payment.completed", "charge.completed.v2"}
                and status == "successful"
                and currency == CURRENCY
                and email
                and provider_tx_ref.startswith("TASKORA-BIZ-")
            ):
                advertiser = conn.execute(
                    "SELECT id,email FROM users WHERE LOWER(email)=? AND role IN ('advertiser','business') LIMIT 1",
                    (email,),
                ).fetchone()
                if advertiser:
                    parts = provider_tx_ref.split("-")
                    task_id = int(parts[2]) if len(parts) >= 4 and parts[2].isdigit() else 0
                    task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, advertiser["id"])).fetchone() if task_id else None
                    if task and amount == float(task["total_budget"] or 0):
                        already = conn.execute("SELECT id FROM payment_events WHERE transaction_id=? AND event_type='business_funding_verified' LIMIT 1", (transaction_id,)).fetchone()
                        if not already:
                            conn.execute("INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"FLW-BIZ-{transaction_id}", advertiser["id"], "business_funding_verified", transaction_id, int(amount), CURRENCY, json.dumps(verified), now()))
                            _ensure_advertiser_wallet(conn, advertiser["id"])
                            conn.execute("UPDATE advertiser_wallets SET balance=balance+?,updated_at=? WHERE user_id=?", (int(amount), now(), advertiser["id"]))
                            _record_advertiser_tx(conn, advertiser["id"], int(amount), f"TASKORA-DEPOSIT-{transaction_id}", f"Campaign funding received: {task['title']}", "funding", task_id)
                            if _reserve_task_from_wallet(conn, task_id, advertiser["id"]):
                                conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (task_id,))

            if (
                event in {"charge.completed", "payment.completed", "charge.completed.v2"}
                and status == "successful"
                and currency == CURRENCY
                and email
                and provider_tx_ref.startswith("TASKORA-BAL-")
            ):
                advertiser = conn.execute(
                    "SELECT id FROM users WHERE LOWER(email)=? AND role IN ('advertiser','business') LIMIT 1",
                    (email,),
                ).fetchone()
                if advertiser:
                    already = conn.execute("SELECT id FROM payment_events WHERE transaction_id=? AND event_type='business_wallet_funding_verified' LIMIT 1", (transaction_id,)).fetchone()
                    if not already:
                        conn.execute("INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"FLW-BAL-{transaction_id}", advertiser["id"], "business_wallet_funding_verified", transaction_id, int(amount), CURRENCY, json.dumps(verified), now()))
                        _ensure_advertiser_wallet(conn, advertiser["id"])
                        conn.execute("UPDATE advertiser_wallets SET balance=balance+?,updated_at=? WHERE user_id=?", (int(amount), now(), advertiser["id"]))
                        _record_advertiser_tx(conn, advertiser["id"], int(amount), f"TASKORA-DEPOSIT-{transaction_id}", "Advertiser wallet funding received", "funding")

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
    if str(u["role"] or "").lower() != "worker":
        if str(u["role"] or "").lower() in ("advertiser", "business"):
            return redirect(url_for("business_dashboard"))
        if str(u["role"] or "").lower() == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    if not u["activated"]:
        flash("Activate your worker account before opening tasks.", "error")
        return redirect(url_for("activate"))
    conn = db()
    rows = conn.execute("SELECT * FROM tasks WHERE status='open' ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("tasks.html", tasks=rows, user=u)


@app.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    u = current_user()
    if str(u["role"] or "").lower() != "worker":
        if str(u["role"] or "").lower() in ("advertiser", "business"):
            return redirect(url_for("business_dashboard"))
        if str(u["role"] or "").lower() == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    if not u["activated"]:
        flash("Activate your worker account before opening this task.", "error")
        return redirect(url_for("activate"))
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
    return render_template("task_detail.html", task=task, already=already, user=u)
@app.route("/tasks/<int:task_id>/submit", methods=["POST"])
@login_required
def submit_task(task_id):
    u = current_user()

    if str(u["role"] or "").lower() != "worker":
        if str(u["role"] or "").lower() in ("advertiser", "business"):
            return redirect(url_for("business_dashboard"))
        return redirect(url_for("admin_dashboard"))

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
        "SELECT w.*, b.bank_name,b.account_number FROM withdrawals w JOIN bank_accounts b ON b.id=w.bank_account_id WHERE w.user_id=? AND w.status <> 'rejected' ORDER BY w.id DESC LIMIT 30",
        (u["id"],)
    ).fetchall()
    banks = conn.execute("SELECT * FROM bank_accounts WHERE user_id=? AND is_deleted=0 ORDER BY id DESC", (u["id"],)).fetchall()
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
                    "INSERT INTO bank_accounts(user_id,bank_code,bank_name,account_number,account_name,is_verified,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (u["id"], bank_code, bank_name, account_number, account_name, 0, "pending", now())
                )
                conn.commit()
                flash("Bank account saved. Verify the account name before withdrawing.", "success")
            except Exception:
                flash("That bank account is already saved.", "error")
    banks = conn.execute("SELECT * FROM bank_accounts WHERE user_id=? AND is_deleted=0 ORDER BY id DESC", (u["id"],)).fetchall()
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
    bank = conn.execute("SELECT * FROM bank_accounts WHERE id=? AND user_id=? AND is_deleted=0", (bank_id, u["id"])).fetchone()
    conn.close()
    if not bank:
        flash("Select a valid bank account first.", "error")
        return redirect(url_for("wallet"))
    if not bank["is_verified"] or (bank["status"] if "status" in bank.keys() else "pending") != "verified":
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
    _expire_advertiser_campaigns(conn)
    stats = {
        "users": conn.execute("SELECT COUNT(*) FROM users WHERE role='worker'").fetchone()[0],
        "activated": conn.execute("SELECT COUNT(*) FROM users WHERE role='worker' AND activated=1").fetchone()[0],
        "businesses": conn.execute("SELECT COUNT(*) FROM users WHERE role IN ('advertiser','business')").fetchone()[0],
        "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
        "business_pending_tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE owner_user_id IS NOT NULL AND status IN ('pending','awaiting_payment')").fetchone()[0],
        "pending_submissions": conn.execute("SELECT COUNT(*) FROM submissions WHERE status='pending'").fetchone()[0],
        "reserved_campaign_funds": conn.execute("SELECT COALESCE(SUM(reserved_balance),0) FROM advertiser_wallets").fetchone()[0],
        "platform_revenue": conn.execute("SELECT COALESCE(SUM(amount),0) FROM platform_revenue").fetchone()[0],
        "pending_withdrawals": conn.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0],
        "paid_withdrawals": conn.execute("SELECT COUNT(*) FROM withdrawals WHERE status='paid'").fetchone()[0],
    }
    recent_withdrawals = conn.execute("""
        SELECT w.id, w.amount, w.net_amount, w.status, w.requested_at, u.full_name, u.email
        FROM withdrawals w
        JOIN users u ON u.id=w.user_id
        ORDER BY w.id DESC LIMIT 20
    """).fetchall()
    conn.close()
    return render_template("admin.html", stats=stats, withdrawals=recent_withdrawals)


@app.route("/admin/tasks")
@admin_required
def admin_tasks():
    """List all tasks for admin management, regardless of deadline."""
    conn = db()
    _expire_advertiser_campaigns(conn)
    tasks = conn.execute("""
        SELECT
            t.*,
            owner.full_name AS owner_name,
            owner.email AS owner_email,
            COUNT(s.id) AS submission_count,
            SUM(CASE WHEN s.status='pending' THEN 1 ELSE 0 END) AS pending_submissions
        FROM tasks t
        LEFT JOIN users owner ON owner.id=t.owner_user_id
        LEFT JOIN submissions s ON s.task_id=t.id
        GROUP BY t.id, owner.full_name, owner.email
        ORDER BY t.id DESC
    """).fetchall()
    conn.close()
    return render_template("admin_tasks.html", tasks=tasks)


@app.route("/admin/tasks/<int:task_id>/approve", methods=["POST"])
@admin_required
def admin_approve_advertiser_task(task_id):
    conn = db()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            flash("Task not found.", "error")
            return redirect(url_for("admin_tasks"))
        if not task["owner_user_id"]:
            flash("This is an admin-created task; no advertiser approval is needed.", "info")
            return redirect(url_for("admin_tasks"))
        if task["payment_status"] != "funded" or int(task["reserved_budget"] or 0) <= 0:
            flash("This advertiser task must be fully funded before it can be published.", "error")
            return redirect(url_for("admin_tasks"))
        conn.execute("UPDATE tasks SET status='open', approved_at=? WHERE id=?", (now(), task_id))
        conn.commit()
        flash("Advertiser task approved and published to workers.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not approve task: {e}", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_tasks"))


@app.route("/admin/tasks/<int:task_id>/delete", methods=["POST"])
@admin_required
def admin_delete_task(task_id):
    """Delete any task at any time, including tasks whose deadline has not passed."""
    conn = db()
    try:
        task = conn.execute("SELECT id, title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            flash("Task not found.", "error")
            return redirect(url_for("admin_tasks"))

        # Return any unused advertiser reserve before deleting a business campaign.
        full_task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if full_task and full_task["owner_user_id"] and int(full_task["reserved_budget"] or 0) > 0:
            _release_task_reserve(conn, full_task, "Unused campaign funds returned after Admin deleted the campaign.")

        # Do not leave pending ledger entries behind when submissions are removed.
        # Approved/available earnings are intentionally preserved.
        submissions = conn.execute(
            "SELECT id, user_id FROM submissions WHERE task_id=?",
            (task_id,),
        ).fetchall()
        for submission in submissions:
            ref = f"TASKORA-EARN-{submission['user_id']}-{submission['id']}"
            conn.execute(
                "UPDATE ledger SET status='rejected', description=? "
                "WHERE reference=? AND user_id=? AND kind='earning' AND status='pending'",
                (f"Task deleted by admin: {task['title']}", ref, submission['user_id']),
            )

        # submissions.task_id uses ON DELETE CASCADE, so related submissions are removed.
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
        flash("Task deleted successfully.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not delete task: {str(e)}", "error")
    finally:
        conn.close()

    return redirect(url_for("admin_tasks"))


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
        # Business-funded campaigns are settled from their reserved balance only
        # when Admin approves the worker proof. Admin-created tasks keep the old
        # worker-only ledger flow.
        task_full = conn.execute("SELECT * FROM tasks WHERE id=?", (s["task_id"],)).fetchone()
        if task_full and task_full["owner_user_id"]:
            _settle_business_submission(conn, task_full, submission_id)

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

        task_full = conn.execute("SELECT * FROM tasks WHERE id=?", (s["task_id"],)).fetchone()
        if task_full and task_full["owner_user_id"] and int(task_full["completed_slots"] or 0) >= int(task_full["slots"] or 0):
            conn.execute("UPDATE tasks SET status='completed', reserved_budget=0, payment_status='completed', closed_at=? WHERE id=?", (now(), task_full["id"]))

        flash("Submission approved and worker earnings credited. Advertiser campaign funds were settled.", "success")
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


@app.route("/admin/ai", methods=["GET", "POST"])
@admin_required
def admin_ai():
    answer = None
    prompt = ""
    if request.method == "POST":
        prompt = request.form.get("prompt", "").strip()
        if not prompt:
            flash("Enter a question or instruction for TASKORA AI.", "error")
        elif len(prompt) > 6000:
            flash("AI request is too long. Please keep it under 6000 characters.", "error")
        else:
            try:
                client = get_openai_client()
                response = client.responses.create(
                    model=OPENAI_MODEL,
                    instructions=(
                        "You are TASKORA AI, the internal assistant for TASKORA WORK. "
                        "Give concise, practical help to the administrator. Do not invent "
                        "financial transactions, user records, or system actions. "
                        "If you do not know something, say so clearly."
                    ),
                    input=prompt,
                )
                answer = response.output_text
            except Exception as e:
                error_text = str(e)
                if "billing_not_active" in error_text or "account is not active" in error_text.lower():
                    flash("TASKORA AI is connected, but OpenAI API billing is not active. Add/activate an API billing method on the OpenAI Platform, then try again.", "error")
                elif "model" in error_text.lower() and "not found" in error_text.lower():
                    flash(f"TASKORA AI model '{OPENAI_MODEL}' is not available for this API project. Set OPENAI_MODEL to an available model.", "error")
                else:
                    flash(f"TASKORA AI error: {error_text}", "error")
    return render_template(
        "admin_ai.html",
        answer=answer,
        prompt=prompt,
        ai_model=OPENAI_MODEL,
        ai_ready=bool(OPENAI_API_KEY and OpenAI is not None),
    )


@app.route("/admin/withdrawals")
@admin_required
def admin_withdrawals():
    conn = db()
    rows = conn.execute("""
        SELECT w.*,u.full_name,u.email,b.bank_name,b.bank_code,b.account_number,b.account_name
        FROM withdrawals w JOIN users u ON u.id=w.user_id
        JOIN bank_accounts b ON b.id=w.bank_account_id
        WHERE w.status <> 'rejected'
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
    try:
        users = conn.execute(
            "SELECT * FROM users WHERE role='worker' ORDER BY id DESC"
        ).fetchall()
        banks = conn.execute(
            "SELECT * FROM bank_accounts WHERE COALESCE(is_deleted,0)=0 ORDER BY id DESC"
        ).fetchall()
        return render_template("admin_users.html", users=users, banks=banks)
    except Exception as e:
        conn.rollback()
        flash(f"Could not load workers and bank accounts: {str(e)}", "error")
        return redirect(url_for("admin_dashboard"))
    finally:
        conn.close()


@app.route("/admin/banks/<int:bank_id>/verify", methods=["POST"])
@admin_required
def admin_verify_bank(bank_id):
    conn = db()
    try:
        bank = conn.execute(
            "SELECT id FROM bank_accounts WHERE id=? AND COALESCE(is_deleted,0)=0",
            (bank_id,),
        ).fetchone()
        if not bank:
            flash("Bank account not found.", "error")
            return redirect(url_for("admin_users"))

        conn.execute(
            "UPDATE bank_accounts SET is_verified=1,status='verified',rejection_note=NULL WHERE id=?",
            (bank_id,),
        )
        conn.commit()
        flash("Bank account verified successfully.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not verify bank account: {str(e)}", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/banks/<int:bank_id>/reject", methods=["POST"])
@admin_required
def admin_reject_bank(bank_id):
    conn = db()
    try:
        bank = conn.execute(
            "SELECT id FROM bank_accounts WHERE id=? AND COALESCE(is_deleted,0)=0",
            (bank_id,),
        ).fetchone()
        if not bank:
            flash("Bank account not found.", "error")
            return redirect(url_for("admin_users"))

        note = request.form.get("note", "Bank account rejected by admin.").strip()[:500]
        conn.execute(
            "UPDATE bank_accounts SET is_verified=0,status='rejected',rejection_note=? WHERE id=?",
            (note, bank_id),
        )
        conn.commit()
        flash("Bank account rejected successfully.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not reject bank account: {str(e)}", "error")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/banks/<int:bank_id>/delete", methods=["POST"])
@admin_required
def admin_delete_bank(bank_id):
    conn = db()
    try:
        bank = conn.execute("SELECT id FROM bank_accounts WHERE id=? AND is_deleted=0", (bank_id,)).fetchone()
        if not bank:
            flash("Bank account not found.", "error")
            return redirect(url_for("admin_users"))

        linked = conn.execute(
            "SELECT COUNT(*) FROM withdrawals WHERE bank_account_id=?",
            (bank_id,),
        ).fetchone()[0]

        if linked:
            # Preserve financial history while removing the account from all
            # normal user/admin views.
            conn.execute("UPDATE bank_accounts SET is_deleted=1 WHERE id=?", (bank_id,))
        else:
            conn.execute("DELETE FROM bank_accounts WHERE id=?", (bank_id,))

        conn.commit()
        flash("Bank account removed successfully.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Could not remove bank account: {str(e)}", "error")
    finally:
        conn.close()
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
    conn.close()
    if user["activated"]:
        flash("This worker is permanently ACTIVE after successful ₦3,000 activation. Admin cannot deactivate or revoke it.", "error")
    else:
        flash("Manual activation is disabled. Worker activation must be confirmed from a successful ₦3,000 Flutterwave payment.", "error")
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


# ---------------- BUSINESS / ADVERTISER ----------------

def _business_guard():
    """Allow only a dedicated advertiser/business account into business routes."""
    user = current_user()
    if not user:
        return None, redirect(url_for("business_login"))
    role = str(user["role"] or "").lower()
    if role not in ("business", "advertiser"):
        if role == "admin":
            return None, redirect(url_for("admin_dashboard"))
        return None, redirect(url_for("dashboard"))
    return user, None


def _ensure_advertiser_wallet(conn, user_id):
    conn.execute(
        "INSERT OR IGNORE INTO advertiser_wallets(user_id,balance,reserved_balance,updated_at) VALUES(?,?,?,?)",
        (user_id, 0, 0, now()),
    )


def _advertiser_wallet(conn, user_id):
    _ensure_advertiser_wallet(conn, user_id)
    return conn.execute(
        "SELECT * FROM advertiser_wallets WHERE user_id=?", (user_id,)
    ).fetchone()


def _campaign_numbers(reward, slots):
    reward = int(reward)
    slots = int(slots)
    worker_budget = reward * slots
    # Fee is charged only on work actually completed. Keeping it per-worker
    # makes the budget deterministic and prevents rounding surprises.
    fee_per_worker = (reward * ADVERTISER_PLATFORM_FEE_PERCENT) // 100
    platform_fee = fee_per_worker * slots
    total_budget = worker_budget + platform_fee
    return worker_budget, platform_fee, total_budget, fee_per_worker


def _record_advertiser_tx(conn, advertiser_id, amount, reference, description, tx_type, task_id=None):
    conn.execute(
        "INSERT OR IGNORE INTO advertiser_transactions(advertiser_id,task_id,type,amount,reference,description,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (advertiser_id, task_id, tx_type, amount, reference, description, "completed", now()),
    )


def _reserve_task_from_wallet(conn, task_id, advertiser_id):
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, advertiser_id)).fetchone()
    if not task:
        raise RuntimeError("Campaign not found.")
    total = int(task["total_budget"] or 0)
    if total <= 0:
        raise RuntimeError("Campaign budget is invalid.")
    wallet = _advertiser_wallet(conn, advertiser_id)
    if int(wallet["balance"] or 0) < total:
        return False
    updated = conn.execute(
        "UPDATE advertiser_wallets SET balance=balance-?, reserved_balance=reserved_balance+?, updated_at=? WHERE user_id=? AND balance>=?",
        (total, total, now(), advertiser_id, total),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        "UPDATE tasks SET payment_status='funded', reserved_budget=?, funded_at=? WHERE id=? AND owner_user_id=?",
        (total, now(), task_id, advertiser_id),
    )
    _record_advertiser_tx(
        conn, advertiser_id, -total, f"TASKORA-RESERVE-{task_id}",
        f"Campaign budget reserved: {task['title']}", "campaign_reserve", task_id,
    )
    return True


def _release_task_reserve(conn, task, reason="Campaign closed; unused funds returned to advertiser wallet."):
    reserved = int(task["reserved_budget"] or 0)
    # If Admin closes/rejects a campaign, pending worker proofs must also be
    # closed so they cannot be approved later against a refunded budget.
    pending_submissions = conn.execute("SELECT id,user_id FROM submissions WHERE task_id=? AND status='pending'", (task["id"],)).fetchall()
    for submission in pending_submissions:
        conn.execute("UPDATE submissions SET status='rejected',reviewer_note=?,reviewed_at=? WHERE id=? AND status='pending'", (reason[:500], now(), submission["id"]))
        ref = f"TASKORA-EARN-{submission['user_id']}-{submission['id']}"
        conn.execute("UPDATE ledger SET status='rejected',description=? WHERE reference=? AND user_id=? AND kind='earning' AND status='pending'", (f"Campaign closed: {task['title']}", ref, submission["user_id"]))

    if reserved <= 0:
        return 0
    advertiser_id = task["owner_user_id"]
    conn.execute(
        "UPDATE advertiser_wallets SET balance=balance+?, reserved_balance=MAX(0,reserved_balance-?), updated_at=? WHERE user_id=?",
        (reserved, reserved, now(), advertiser_id),
    )
    ref = f"TASKORA-REFUND-{task['id']}"
    _record_advertiser_tx(conn, advertiser_id, reserved, ref, reason, "campaign_refund", task["id"])
    conn.execute(
        "UPDATE tasks SET reserved_budget=0,payment_status='refunded',closed_at=?,refund_reference=? WHERE id=?",
        (now(), ref, task["id"]),
    )
    return reserved


def _expire_advertiser_campaigns(conn):
    """Close expired funded campaigns once no worker proof is awaiting review."""
    rows = conn.execute("SELECT * FROM tasks WHERE owner_user_id IS NOT NULL AND status IN ('open','pending') AND deadline IS NOT NULL AND deadline <> ''").fetchall()
    for task in rows:
        try:
            raw = str(task["deadline"])
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LAGOS_TZ)
            if dt.astimezone(LAGOS_TZ) >= lagos_now():
                continue
        except Exception:
            continue
        pending = conn.execute("SELECT COUNT(*) FROM submissions WHERE task_id=? AND status='pending'", (task["id"],)).fetchone()[0]
        if pending:
            continue
        _release_task_reserve(conn, task, "Campaign deadline passed; unused funds returned to advertiser wallet.")
        conn.execute("UPDATE tasks SET status='expired' WHERE id=?", (task["id"],))
    conn.commit()


def _record_platform_revenue(conn, task_id, advertiser_id, submission_id, amount, reference):
    conn.execute(
        "INSERT OR IGNORE INTO platform_revenue(task_id,advertiser_id,submission_id,amount,reference,created_at) VALUES(?,?,?,?,?,?)",
        (task_id, advertiser_id, submission_id, int(amount), reference, now()),
    )


def _settle_business_submission(conn, task, submission_id):
    """Charge the advertiser only when Admin approves a worker submission."""
    reward = int(task["reward"] or 0)
    _, _, _, fee_per_worker = _campaign_numbers(reward, 1)
    charge = reward + fee_per_worker
    reserved = int(task["reserved_budget"] or 0)
    if reserved < charge:
        raise RuntimeError("This campaign no longer has enough reserved funds.")

    updated = conn.execute(
        "UPDATE advertiser_wallets SET reserved_balance=reserved_balance-?, updated_at=? WHERE user_id=? AND reserved_balance>=?",
        (charge, now(), task["owner_user_id"], charge),
    )
    if updated.rowcount != 1:
        raise RuntimeError("Advertiser campaign funds could not be settled.")

    conn.execute(
        "UPDATE tasks SET reserved_budget=reserved_budget-?, completed_slots=completed_slots+1 WHERE id=?",
        (charge, task["id"]),
    )
    settlement_ref = f"TASKORA-SETTLE-{task['id']}-{submission_id}"
    _record_advertiser_tx(
        conn, task["owner_user_id"], -charge, settlement_ref,
        f"Worker reward + {ADVERTISER_PLATFORM_FEE_PERCENT}% platform fee settled: {task['title']}",
        "worker_settlement", task["id"],
    )
    _record_platform_revenue(
        conn, task["id"], task["owner_user_id"], submission_id, fee_per_worker,
        f"TASKORA-FEE-{task['id']}-{submission_id}",
    )


def _advertiser_metrics(user_id):
    conn = db()
    _expire_advertiser_campaigns(conn)
    wallet = _advertiser_wallet(conn, user_id)
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE owner_user_id=? ORDER BY id DESC", (user_id,)
    ).fetchall()
    pending_reviews = conn.execute("""
        SELECT COUNT(*) FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE t.owner_user_id=? AND s.status='pending'
    """, (user_id,)).fetchone()[0]
    approved = conn.execute("""
        SELECT COUNT(*) FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE t.owner_user_id=? AND s.status='approved'
    """, (user_id,)).fetchone()[0]
    rejected = conn.execute("""
        SELECT COUNT(*) FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE t.owner_user_id=? AND s.status='rejected'
    """, (user_id,)).fetchone()[0]
    pending_value = conn.execute("""
        SELECT COALESCE(SUM(t.reward),0) FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE t.owner_user_id=? AND s.status='pending'
    """, (user_id,)).fetchone()[0]
    approved_value = conn.execute("""
        SELECT COALESCE(SUM(t.reward),0) FROM submissions s JOIN tasks t ON t.id=s.task_id
        WHERE t.owner_user_id=? AND s.status='approved'
    """, (user_id,)).fetchone()[0]
    total_budget = conn.execute("SELECT COALESCE(SUM(total_budget),0) FROM tasks WHERE owner_user_id=?", (user_id,)).fetchone()[0]
    reserved = int(wallet["reserved_balance"] or 0)
    active = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE owner_user_id=? AND status='open'", (user_id,)
    ).fetchone()[0]
    completed = conn.execute(
        "SELECT COALESCE(SUM(completed_slots),0) FROM tasks WHERE owner_user_id=?", (user_id,)
    ).fetchone()[0]
    conn.close()
    total_submissions = approved + rejected + pending_reviews
    conversion = round((approved / total_submissions) * 100, 1) if total_submissions else 0
    budget_used = max(0, int(total_budget or 0) - reserved)
    budget_percent = round((budget_used / int(total_budget)) * 100, 1) if total_budget else 0
    return {
        "tasks": tasks,
        "available_budget": int(wallet["balance"] or 0),
        "reserved_budget": reserved,
        "active_campaigns": active,
        "total_reach": completed,
        "pending_reviews": pending_reviews,
        "budget_used_percent": min(100, budget_percent),
        "approved_submissions": approved,
        "rejected_submissions": rejected,
        "conversion_rate": conversion,
        "pending_value": int(pending_value or 0),
        "approved_value": int(approved_value or 0),
        "total_budget": int(total_budget or 0),
    }


@app.route("/business/register", methods=["GET", "POST"])
def business_register():
    if request.method == "POST":
        business_name = request.form.get("business_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        if len(business_name) < 2 or not email or len(phone) < 7 or len(password) < 8:
            flash("Fill all fields correctly. Password must be at least 8 characters.", "error")
            return render_template("business/register.html")
        conn = db()
        try:
            code = None
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            for _ in range(20):
                candidate = "ADV-" + "".join(secrets.choice(alphabet) for _ in range(8))
                if not conn.execute("SELECT 1 FROM users WHERE referral_code=?", (candidate,)).fetchone():
                    code = candidate
                    break
            conn.execute(
                "INSERT INTO users(full_name,email,phone,password_hash,role,activated,referral_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (business_name, email, phone, generate_password_hash(password), "advertiser", 1, code, now()),
            )
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            _ensure_advertiser_wallet(conn, user["id"])
            conn.commit()
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("business_dashboard"))
        except Exception:
            conn.rollback()
            flash("Email or phone number is already registered.", "error")
            return render_template("business/register.html")
        finally:
            conn.close()
    return render_template("business/register.html")


@app.route("/business/login", methods=["GET", "POST"])
def business_login():
    # Advertiser, Worker and Admin accounts all use the same secure sign-in.
    # The role stored on the account decides which dashboard opens.
    return redirect(url_for("login"))


@app.route("/business/dashboard")
def business_dashboard():
    user, response = _business_guard()
    if response:
        return response
    return render_template("business/dashboard.html", user=user, fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT, **_advertiser_metrics(user["id"]))


@app.route("/business/tasks/new", methods=["GET", "POST"])
def business_create_task():
    user, response = _business_guard()
    if response:
        return response
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()
        task_link = request.form.get("task_link", "").strip() or request.form.get("link", "").strip()
        description = request.form.get("description", "").strip()
        try:
            reward = int(request.form.get("reward", "0") or 0)
            slots = int(request.form.get("slots", "0") or 0)
        except ValueError:
            reward, slots = 0, 0
        deadline = request.form.get("deadline", "").strip()
        if not title or not category or not description or reward <= 0 or slots <= 0:
            flash("Complete all task fields correctly.", "error")
            return render_template("business/create_task.html", fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)
        if task_link and not task_link.startswith(("http://", "https://")):
            flash("Task link must start with http:// or https://.", "error")
            return render_template("business/create_task.html", fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)

        worker_budget, platform_fee, total_budget, fee_per_worker = _campaign_numbers(reward, slots)
        if total_budget < ADVERTISER_MIN_FUNDING:
            flash(f"Campaign total must be at least ₦{ADVERTISER_MIN_FUNDING:,}.", "error")
            return render_template("business/create_task.html", fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)

        conn = db()
        try:
            conn.execute(
                "INSERT INTO tasks(owner_user_id,title,category,description,task_link,reward,deadline,slots,difficulty,status,created_at,payment_status,worker_budget,platform_fee,total_budget,reserved_budget,completed_slots) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user["id"], title, category, description, task_link or None, reward, deadline or None, slots, "Beginner", "awaiting_payment", now(), "unfunded", worker_budget, platform_fee, total_budget, 0, 0),
            )
            task = conn.execute("SELECT * FROM tasks WHERE owner_user_id=? ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
            if _reserve_task_from_wallet(conn, task["id"], user["id"]):
                conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (task["id"],))
                conn.commit()
                flash("Campaign budget reserved. Please review the task and submit it to TASKORA admin for approval.", "success")
                return redirect(url_for("business_dashboard"))
            conn.commit()
            flash("Task created. Fund the campaign first; it will go to Admin for approval after payment is confirmed.", "info")
            return redirect(url_for("business_fund_task", task_id=task["id"]))
        except Exception as e:
            conn.rollback()
            flash(f"Could not create campaign: {e}", "error")
        finally:
            conn.close()
    return render_template("business/create_task.html", fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)


@app.route("/business/tasks")
def business_tasks():
    user, response = _business_guard()
    if response:
        return response
    return redirect(url_for("business_dashboard"))


@app.route("/business/tasks/<int:task_id>/fund", methods=["GET", "POST"])
def business_fund_task(task_id):
    user, response = _business_guard()
    if response:
        return response
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, user["id"])).fetchone()
    wallet = _advertiser_wallet(conn, user["id"])
    conn.close()
    if not task:
        flash("Campaign not found.", "error")
        return redirect(url_for("business_dashboard"))
    if task["payment_status"] == "funded":
        flash("This campaign is already funded and reserved.", "success")
        return redirect(url_for("business_dashboard"))

    if request.method == "POST":
        if not FLW_SECRET_KEY:
            flash("Flutterwave is not configured on the server. Add FLW_SECRET_KEY in Render Environment.", "error")
            return redirect(url_for("business_fund_task", task_id=task_id))
        tx_ref = f"TASKORA-BIZ-{task_id}-{uuid.uuid4().hex[:16]}"
        redirect_url = f"{BASE_URL}/business/payment/callback"
        payload = {
            "tx_ref": tx_ref,
            "amount": int(task["total_budget"]),
            "currency": CURRENCY,
            "redirect_url": redirect_url,
            "payment_options": FLW_PAYMENT_OPTIONS,
        "bank_transfer_options": {"expires": 3600},
            "customer": {"email": user["email"], "name": user["full_name"], "phonenumber": user["phone"]},
            "customizations": {"title": "TASKORA WORK Campaign Funding", "description": f"Fund campaign: {task['title']}"},
            "meta": {"taskora_type": "advertiser_campaign", "task_id": task_id, "advertiser_id": user["id"]},
        }
        try:
            result = flw_post("/payments", payload)
            checkout_link = str((result.get("data") or {}).get("link") or "").strip()
            if not checkout_link:
                raise RuntimeError("Flutterwave did not return a checkout link.")
            conn = db()
            conn.execute(
                "INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (tx_ref, user["id"], "business_funding_started", int(task["total_budget"]), CURRENCY, json.dumps({"task_id": task_id, "flutterwave_response": result}), now()),
            )
            conn.commit()
            conn.close()
            return redirect(checkout_link)
        except Exception as e:
            flash(str(e), "error")
            return redirect(url_for("business_fund_task", task_id=task_id))

    worker_budget, platform_fee, total_budget, _ = _campaign_numbers(task["reward"], task["slots"])
    return render_template("business/fund_task.html", task=task, wallet=wallet, worker_budget=worker_budget, platform_fee=platform_fee, total_budget=total_budget, fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)


@app.route("/business/payment/callback")
def business_payment_callback():
    status = str(request.args.get("status") or "").lower()
    transaction_id = request.args.get("transaction_id") or request.args.get("transactionId")
    if status != "successful" or not transaction_id:
        flash("Payment was not completed.", "error")
        return redirect(url_for("business_dashboard"))
    u = current_user()
    if not u or str(u["role"] or "").lower() not in ("advertiser", "business"):
        flash("Please log in to your advertiser account to continue.", "error")
        return redirect(url_for("business_login"))

    try:
        verified = flw_get(f"/transactions/{str(transaction_id).strip()}/verify")
        tx = verified.get("data") or {}
        if str(tx.get("status") or "").lower() != "successful":
            raise RuntimeError("Payment is not successful.")
        if str(tx.get("currency") or "").upper() != CURRENCY:
            raise RuntimeError("Invalid payment currency.")
        amount = int(float(tx.get("amount") or 0))
        email = str((tx.get("customer") or {}).get("email") or "").strip().lower()
        if email != str(u["email"] or "").strip().lower():
            raise RuntimeError("Payment email does not match your advertiser account.")
        provider_ref = str(tx.get("tx_ref") or "").strip()
        if not provider_ref.startswith("TASKORA-BIZ-"):
            raise RuntimeError("This payment is not a TASKORA campaign payment.")
        try:
            task_id = int(provider_ref.split("-")[2])
        except Exception:
            raise RuntimeError("Invalid TASKORA campaign payment reference.")

        conn = db()
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, u["id"])).fetchone()
        if not task:
            conn.close()
            raise RuntimeError("Campaign not found for this payment.")
        expected = int(task["total_budget"] or 0)
        if amount != expected:
            conn.close()
            raise RuntimeError(f"Invalid campaign payment amount. Expected ₦{expected:,}.")
        used = conn.execute("SELECT id FROM payment_events WHERE transaction_id=? AND event_type='business_funding_verified' LIMIT 1", (str(transaction_id),)).fetchone()
        if used:
            conn.close()
            flash("This campaign payment was already verified.", "success")
            return redirect(url_for("business_dashboard"))

        conn.execute(
            "INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"FLW-BIZ-{transaction_id}", u["id"], "business_funding_verified", str(transaction_id), amount, CURRENCY, json.dumps(verified), now()),
        )
        _ensure_advertiser_wallet(conn, u["id"])
        conn.execute("UPDATE advertiser_wallets SET balance=balance+?,updated_at=? WHERE user_id=?", (amount, now(), u["id"]))
        _record_advertiser_tx(conn, u["id"], amount, f"TASKORA-DEPOSIT-{transaction_id}", f"Campaign funding received: {task['title']}", "funding", task_id)
        if not _reserve_task_from_wallet(conn, task_id, u["id"]):
            raise RuntimeError("Payment was received, but campaign reservation could not be completed. Contact TASKORA support before paying again.")
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
        flash("Payment confirmed. Campaign funds are reserved and the task is now waiting for Admin approval.", "success")
        return redirect(url_for("business_dashboard"))
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        flash(str(e), "error")
        return redirect(url_for("business_dashboard"))


@app.route("/business/wallet")
def business_wallet():
    user, response = _business_guard()
    if response:
        return response
    conn = db()
    wallet = _advertiser_wallet(conn, user["id"])
    transactions = conn.execute("SELECT * FROM advertiser_transactions WHERE advertiser_id=? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()
    conn.commit()
    conn.close()
    return render_template("business/wallet.html", user=user, wallet=wallet, transactions=transactions, fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT)


@app.route("/business/wallet/fund", methods=["POST"])
def business_wallet_fund():
    user, response = _business_guard()
    if response:
        return response
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount < ADVERTISER_MIN_FUNDING:
        flash(f"Minimum funding is ₦{ADVERTISER_MIN_FUNDING:,}.", "error")
        return redirect(url_for("business_wallet"))
    if not FLW_SECRET_KEY:
        flash("Flutterwave is not configured on the server.", "error")
        return redirect(url_for("business_wallet"))
    tx_ref = f"TASKORA-BAL-{user['id']}-{uuid.uuid4().hex[:16]}"
    try:
        result = flw_post("/payments", {
            "tx_ref": tx_ref, "amount": amount, "currency": CURRENCY,
            "redirect_url": f"{BASE_URL}/business/payment/wallet-callback",
            "payment_options": FLW_PAYMENT_OPTIONS,
        "bank_transfer_options": {"expires": 3600},
            "customer": {"email": user["email"], "name": user["full_name"], "phonenumber": user["phone"]},
            "customizations": {"title": "TASKORA WORK Advertiser Wallet", "description": "Add campaign funds"},
            "meta": {"taskora_type": "advertiser_wallet", "advertiser_id": user["id"]},
        })
        link = str((result.get("data") or {}).get("link") or "").strip()
        if not link:
            raise RuntimeError("Flutterwave did not return a checkout link.")
        conn = db()
        conn.execute("INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?)", (tx_ref, user["id"], "business_wallet_funding_started", amount, CURRENCY, json.dumps(result), now()))
        conn.commit(); conn.close()
        return redirect(link)
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("business_wallet"))


@app.route("/business/payment/wallet-callback")
def business_wallet_callback():
    status = str(request.args.get("status") or "").lower()
    transaction_id = request.args.get("transaction_id") or request.args.get("transactionId")
    if status != "successful" or not transaction_id:
        flash("Payment was not completed.", "error")
        return redirect(url_for("business_wallet"))
    u = current_user()
    if not u or str(u["role"] or "").lower() not in ("advertiser", "business"):
        return redirect(url_for("business_login"))
    try:
        verified = flw_get(f"/transactions/{str(transaction_id).strip()}/verify")
        tx = verified.get("data") or {}
        if str(tx.get("status") or "").lower() != "successful":
            raise RuntimeError("Payment is not successful.")
        if str(tx.get("currency") or "").upper() != CURRENCY:
            raise RuntimeError("Invalid payment currency.")
        amount = int(float(tx.get("amount") or 0))
        email = str((tx.get("customer") or {}).get("email") or "").strip().lower()
        if email != str(u["email"] or "").strip().lower():
            raise RuntimeError("Payment email does not match your advertiser account.")
        provider_ref = str(tx.get("tx_ref") or "")
        if not provider_ref.startswith("TASKORA-BAL-"):
            raise RuntimeError("This payment is not a TASKORA wallet payment.")
        conn = db()
        used = conn.execute("SELECT id FROM payment_events WHERE transaction_id=? AND event_type='business_wallet_funding_verified' LIMIT 1", (str(transaction_id),)).fetchone()
        if used:
            conn.close(); flash("This wallet payment was already verified.", "success"); return redirect(url_for("business_wallet"))
        conn.execute("INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"FLW-BAL-{transaction_id}", u["id"], "business_wallet_funding_verified", str(transaction_id), amount, CURRENCY, json.dumps(verified), now()))
        _ensure_advertiser_wallet(conn, u["id"])
        conn.execute("UPDATE advertiser_wallets SET balance=balance+?,updated_at=? WHERE user_id=?", (amount, now(), u["id"]))
        _record_advertiser_tx(conn, u["id"], amount, f"TASKORA-DEPOSIT-{transaction_id}", "Advertiser wallet funding received", "funding")
        conn.commit(); conn.close()
        flash(f"₦{amount:,} added to your campaign wallet.", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("business_wallet"))


@app.route("/business/transactions")
def business_transactions():
    return redirect(url_for("business_wallet"))


@app.route("/business/submissions")
def business_submissions():
    user, response = _business_guard()
    if response:
        return response
    conn = db()
    rows = conn.execute("""
        SELECT s.*, t.title, t.reward, t.category, t.status AS task_status, u.full_name, u.email
        FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN users u ON u.id=s.user_id
        WHERE t.owner_user_id=? ORDER BY s.id DESC LIMIT 100
    """, (user["id"],)).fetchall()
    conn.close()
    return render_template("business/submissions.html", user=user, submissions=rows)


@app.route("/business/analytics")
def business_analytics():
    user, response = _business_guard()
    if response:
        return response
    return render_template("business/analytics.html", user=user, fee_percent=ADVERTISER_PLATFORM_FEE_PERCENT, **_advertiser_metrics(user["id"]))


@app.route("/business/tasks/<int:task_id>/close", methods=["POST"])
def business_close_task(task_id):
    user, response = _business_guard()
    if response:
        return response
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id=?", (task_id, user["id"])).fetchone()
    if not task:
        conn.close(); flash("Campaign not found.", "error"); return redirect(url_for("business_dashboard"))
    if task["status"] not in ("pending", "open"):
        conn.close(); flash("This campaign cannot be closed at its current stage.", "error"); return redirect(url_for("business_dashboard"))
    pending = conn.execute("SELECT COUNT(*) FROM submissions WHERE task_id=? AND status='pending'", (task_id,)).fetchone()[0]
    if pending:
        conn.close(); flash("This campaign has worker submissions waiting for Admin review. Wait until they are resolved before closing it.", "error"); return redirect(url_for("business_dashboard"))
    returned = _release_task_reserve(conn, task, "Unused campaign funds returned after advertiser closed the campaign.")
    conn.execute("UPDATE tasks SET status='closed' WHERE id=?", (task_id,))
    conn.commit(); conn.close()
    flash(f"Campaign closed. ₦{returned:,} unused funds returned to your advertiser wallet.", "success")
    return redirect(url_for("business_dashboard"))


# ---------------- ADMIN BUSINESS CONTROL ----------------

@app.route("/admin/businesses")
@admin_required
def admin_businesses():
    conn = db()
    businesses = conn.execute("""
        SELECT u.id,u.full_name,u.email,u.phone,u.created_at,
               COALESCE(w.balance,0) AS wallet_balance, COALESCE(w.reserved_balance,0) AS reserved_balance,
               COUNT(t.id) AS task_count
        FROM users u
        LEFT JOIN advertiser_wallets w ON w.user_id=u.id
        LEFT JOIN tasks t ON t.owner_user_id=u.id
        WHERE u.role IN ('advertiser','business')
        GROUP BY u.id,u.full_name,u.email,u.phone,u.created_at,w.balance,w.reserved_balance
        ORDER BY u.id DESC
    """).fetchall()
    conn.close()
    return render_template("admin_businesses.html", businesses=businesses)


@app.route("/admin/tasks/<int:task_id>/reject", methods=["POST"])
@admin_required
def admin_reject_advertiser_task(task_id):
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or not task["owner_user_id"]:
        conn.close(); flash("Advertiser campaign not found.", "error"); return redirect(url_for("admin_tasks"))
    if task["status"] not in ("pending", "awaiting_payment"):
        conn.close(); flash("This campaign cannot be rejected at its current stage.", "error"); return redirect(url_for("admin_tasks"))
    note = request.form.get("note", "Campaign rejected by TASKORA admin.").strip()[:500]
    if task["reserved_budget"]:
        _release_task_reserve(conn, task, f"Campaign rejected by Admin: {note}")
    conn.execute("UPDATE tasks SET status='rejected' WHERE id=?", (task_id,))
    conn.commit(); conn.close()
    flash("Advertiser campaign rejected. Any reserved unused funds were returned to the advertiser wallet.", "success")
    return redirect(url_for("admin_tasks"))



@app.route("/admin/tasks/<int:task_id>/close", methods=["POST"])
@admin_required
def admin_close_advertiser_task(task_id):
    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND owner_user_id IS NOT NULL", (task_id,)).fetchone()
    if not task:
        conn.close(); flash("Advertiser campaign not found.", "error"); return redirect(url_for("admin_tasks"))
    if task["status"] in ("completed", "refunded", "rejected"):
        conn.close(); flash("Campaign is already closed.", "error"); return redirect(url_for("admin_tasks"))
    returned = _release_task_reserve(conn, task, "Unused campaign funds returned after Admin closed the campaign.")
    conn.execute("UPDATE tasks SET status='closed' WHERE id=?", (task_id,))
    conn.commit(); conn.close()
    flash(f"Campaign closed. ₦{returned:,} unused funds returned to advertiser wallet.", "success")
    return redirect(url_for("admin_tasks"))

