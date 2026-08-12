import os
import sqlite3
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "taskora.db")

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


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        bank_code TEXT NOT NULL,
        bank_name TEXT NOT NULL,
        account_number TEXT NOT NULL,
        account_name TEXT,
        is_verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, account_number),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        reward INTEGER NOT NULL,
        deadline TEXT,
        slots INTEGER NOT NULL DEFAULT 1,
        difficulty TEXT NOT NULL DEFAULT 'Beginner',
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        proof TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reviewer_note TEXT,
        submitted_at TEXT NOT NULL,
        reviewed_at TEXT,
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount INTEGER NOT NULL,
        reference TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'available',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        bank_account_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        fee INTEGER NOT NULL DEFAULT 0,
        net_amount INTEGER NOT NULL,
        reference TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_transfer_id TEXT,
        note TEXT,
        requested_at TEXT NOT NULL,
        processed_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(bank_account_id) REFERENCES bank_accounts(id)
    );

    CREATE TABLE IF NOT EXISTS payment_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_ref TEXT UNIQUE NOT NULL,
        user_id INTEGER,
        event_type TEXT NOT NULL,
        transaction_id TEXT,
        amount INTEGER,
        currency TEXT,
        raw_json TEXT,
        created_at TEXT NOT NULL
    );
    """)
    # Create an admin account only if none exists. Change the password immediately.
    admin = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO users(full_name,email,phone,password_hash,role,activated,created_at) VALUES(?,?,?,?,?,?,?)",
            ("TASKORA Admin", "admin@taskora.local", "0000000000",
             generate_password_hash(os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")),
             "admin", 1, now())
        )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


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
        except sqlite3.IntegrityError:
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
    u = current_user()
    if u["activated"]:
        return redirect(url_for("dashboard"))

    tx_ref = f"TASKORA-ACT-{u['id']}-{uuid.uuid4().hex[:12]}"
    payload = {
        "tx_ref": tx_ref,
        "amount": ACTIVATION_FEE,
        "currency": CURRENCY,
        "redirect_url": f"{BASE_URL}{url_for('activation_callback')}",
        "payment_options": "card,banktransfer,ussd",
        "customer": {
            "email": u["email"],
            "phonenumber": u["phone"],
            "name": u["full_name"]
        },
        "customizations": {
            "title": "TASKORA WORK Activation",
            "description": "Worker account activation fee"
        },
        "meta": {
            "user_id": u["id"],
            "purpose": "account_activation"
        }
    }
    try:
        result = flw_post("/payments", payload)
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("activate"))

    conn = db()
    conn.execute("UPDATE users SET activation_tx_ref=? WHERE id=?", (tx_ref, u["id"]))
    conn.execute(
        "INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (tx_ref, u["id"], "activation_started", ACTIVATION_FEE, CURRENCY, json.dumps(result), now())
    )
    conn.commit()
    conn.close()
    return redirect(result["data"]["link"])


@app.route("/activate/callback")
@login_required
def activation_callback():
    status = request.args.get("status")
    transaction_id = request.args.get("transaction_id")
    tx_ref = request.args.get("tx_ref")
    u = current_user()

    if status != "successful" or not transaction_id:
        flash("Payment was not completed.", "error")
        return redirect(url_for("activate"))

    try:
        r = requests.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers=flw_headers(), timeout=30
        )
        data = r.json()
        if r.status_code >= 400 or data.get("status") != "success":
            raise RuntimeError("Could not verify payment.")
        tx = data.get("data", {})
        if (
            tx.get("status") == "successful"
            and int(float(tx.get("amount", 0))) == ACTIVATION_FEE
            and tx.get("currency") == CURRENCY
            and tx.get("tx_ref") == u["activation_tx_ref"]
        ):
            conn = db()
            conn.execute(
                "UPDATE users SET activated=1, activation_transaction_id=? WHERE id=?",
                (str(transaction_id), u["id"])
            )
            conn.execute(
                "INSERT OR IGNORE INTO payment_events(tx_ref,user_id,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (tx_ref or u["activation_tx_ref"], u["id"], "activation_verified",
                 str(transaction_id), ACTIVATION_FEE, CURRENCY, json.dumps(data), now())
            )
            conn.commit()
            conn.close()
            flash("Account activated successfully.", "success")
            return redirect(url_for("dashboard"))
    except Exception as e:
        flash(str(e), "error")

    return redirect(url_for("activate"))


@app.route("/webhooks/flutterwave", methods=["POST"])
def flutterwave_webhook():
    if FLW_WEBHOOK_HASH:
        supplied = request.headers.get("verif-hash", "")
        if not secrets.compare_digest(supplied, FLW_WEBHOOK_HASH):
            return jsonify({"ok": False}), 401

    payload = request.get_json(silent=True) or {}
    event = payload.get("event") or payload.get("event_type") or "unknown"
    data = payload.get("data") or {}
    tx_ref = data.get("tx_ref") or payload.get("tx_ref")

    conn = db()
    if tx_ref:
        conn.execute(
            "INSERT OR IGNORE INTO payment_events(tx_ref,event_type,transaction_id,amount,currency,raw_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (tx_ref, str(event), str(data.get("id") or ""), data.get("amount"),
             data.get("currency"), json.dumps(payload), now())
        )
    conn.commit()
    conn.close()
    return jsonify({"received": True})


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
    if len(proof) < 5:
        flash("Please provide task proof/details.", "error")
        return redirect(url_for("task_detail", task_id=task_id))

    conn = db()
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND status='open'", (task_id,)).fetchone()
    existing = conn.execute(
        "SELECT id FROM submissions WHERE task_id=? AND user_id=? AND status IN ('pending','approved')",
        (task_id, u["id"])
    ).fetchone()
    if not task or existing:
        conn.close()
        flash("Task unavailable or already submitted.", "error")
        return redirect(url_for("tasks"))

    conn.execute(
        "INSERT INTO submissions(task_id,user_id,proof,submitted_at) VALUES(?,?,?,?)",
        (task_id, u["id"], proof, now())
    )
    conn.commit()
    conn.close()
    flash("Task submitted for review.", "success")
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
            except sqlite3.IntegrityError:
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
    if datetime.now(timezone.utc).weekday() != 4:
        flash("Weekly withdrawal requests open on Friday. Your balance remains safe in your wallet.", "error")
        return redirect(url_for("wallet"))

    conn = db()
    bank = conn.execute("SELECT * FROM bank_accounts WHERE id=? AND user_id=?", (bank_id, u["id"])).fetchone()
    conn.close()
    if not bank:
        flash("Select a valid bank account first.", "error")
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
        title = request.form.get("title","").strip()
        category = request.form.get("category","").strip()
        description = request.form.get("description","").strip()
        reward = int(request.form.get("reward","0") or 0)
        deadline = request.form.get("deadline","").strip()
        slots = int(request.form.get("slots","1") or 1)
        difficulty = request.form.get("difficulty","Beginner")
        if not title or not category or not description or reward <= 0:
            flash("Complete all task fields.", "error")
        else:
            conn = db()
            conn.execute(
                "INSERT INTO tasks(title,category,description,reward,deadline,slots,difficulty,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (title,category,description,reward,deadline,slots,difficulty,now())
            )
            conn.commit()
            conn.close()
            flash("Task created.", "success")
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

    if action == "approve":
        ref = f"TASKORA-EARN-{s['user_id']}-{s['id']}"
        conn.execute("UPDATE submissions SET status='approved',reviewed_at=? WHERE id=?", (now(), submission_id))
        conn.execute(
            "INSERT OR IGNORE INTO ledger(user_id,kind,amount,reference,description,status,created_at) VALUES(?,?,?,?,?,?,?)",
            (s["user_id"],"earning",s["reward"],ref,f"Approved task: {s['title']}","available",now())
        )
        flash("Submission approved and earnings credited.", "success")
    else:
        note = request.form.get("note","Task submission rejected.")
        conn.execute(
            "UPDATE submissions SET status='rejected',reviewer_note=?,reviewed_at=? WHERE id=?",
            (note,now(),submission_id)
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


@app.route("/health")
def health():
    return jsonify({"status":"ok","service":"taskora-work"})


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
