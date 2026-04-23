"""Flask-Login wiring — user model, login/signup/logout views."""
from __future__ import annotations

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
        self.stripe_customer_id = row["stripe_customer_id"]

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
