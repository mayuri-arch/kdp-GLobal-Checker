"""Flask-Login wiring — user model, login/signup/logout views."""
from __future__ import annotations

import os
import secrets
import time

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from email_validator import EmailNotValidError, validate_email

from kdp_checker import storage


login_manager = LoginManager()
login_manager.login_view = "auth.login"
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.user_id = row["id"]
        self.email = row["email"]
        self.name = row["name"]
        self.plan = row["plan"]
        self.razorpay_customer_id = row["razorpay_customer_id"]
        self.razorpay_subscription_id = row["razorpay_subscription_id"]
        self.subscription_status = row["subscription_status"]
        self.subscription_start = row["subscription_start"]
        self.subscription_end = row["subscription_end"]
        self.last_payment_at = row["last_payment_at"]
        self.plan_type = row["plan_type"]

    @classmethod
    def from_id(cls, user_id: int):
        with storage.connect() as conn:
            row = storage.get_user(conn, int(user_id))
        return cls(row) if row else None


@login_manager.user_loader
def _load_user(user_id):
    return User.from_id(user_id)


@auth_bp.get("/login")
def login():
    return render_template("login.html")


@auth_bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    with storage.connect() as conn:
        row = storage.get_user_by_email(conn, email)
    if not row or not check_password_hash(row["password_hash"], password):
        flash("Invalid credentials", "error")
        return redirect(url_for("auth.login"))
    login_user(User(row), remember=True)
    return redirect(url_for("index"))


@auth_bp.get("/signup")
def signup():
    return render_template("signup.html")


@auth_bp.post("/signup")
def signup_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    name = (request.form.get("name") or "").strip() or None
    if len(password) < 8:
        flash("Password must be at least 8 characters", "error")
        return redirect(url_for("auth.signup"))
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as e:
        flash(str(e), "error")
        return redirect(url_for("auth.signup"))

    try:
        with storage.connect() as conn:
            user_id = storage.create_user(conn, email, generate_password_hash(password), name)
            row = storage.get_user(conn, user_id)
    except Exception:
        flash("Email already registered", "error")
        return redirect(url_for("auth.signup"))
    login_user(User(row), remember=True)
    return redirect(url_for("index"))


@auth_bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.get("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")


@auth_bp.post("/forgot-password")
def forgot_password_post():
    email = (request.form.get("email") or "").strip().lower()
    with storage.connect() as conn:
        user = storage.get_user_by_email(conn, email)
        if not user:
            # Standard secure warning mitigation (avoid leaking registered emails)
            flash("If that email is registered, a password recovery link has been generated.", "info")
            return redirect(url_for("auth.login"))
            
        token = secrets.token_hex(16)
        expiry = int(time.time()) + 3600
        
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE id = ?",
            (token, expiry, user["id"])
        )
        
    reset_link = url_for("auth.reset_password", token=token, _external=True)
    print(f"\n========================================\nPASSWORD RECOVERY LINK:\n{reset_link}\n========================================\n", flush=True)
    
    flash("A password recovery link has been generated in the logs.", "success")
    is_prod = os.environ.get("FLASK_ENV") == "production" or os.environ.get("DATABASE_URL") is not None
    if not is_prod:
         flash(f"Dev recovery link (logged): {reset_link}", "info")
         
    return redirect(url_for("auth.login"))


@auth_bp.get("/reset-password/<token>")
def reset_password(token):
    with storage.connect() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE reset_token = ? AND reset_token_expiry > ?",
            (token, int(time.time()))
        ).fetchone()
    if not user:
        flash("Invalid or expired recovery token", "error")
        return redirect(url_for("auth.login"))
    return render_template("reset_password.html", token=token)


@auth_bp.post("/reset-password/<token>")
def reset_password_post(token):
    password = request.form.get("password") or ""
    if len(password) < 8:
        flash("Password must be at least 8 characters", "error")
        return redirect(url_for("auth.reset_password", token=token))
        
    with storage.connect() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE reset_token = ? AND reset_token_expiry > ?",
            (token, int(time.time()))
        ).fetchone()
        if not user:
            flash("Invalid or expired recovery token", "error")
            return redirect(url_for("auth.login"))
            
        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expiry = NULL WHERE id = ?",
            (generate_password_hash(password), user["id"])
        )
        
    flash("Password reset successful! You can now log in with your new password.", "success")
    return redirect(url_for("auth.login"))
