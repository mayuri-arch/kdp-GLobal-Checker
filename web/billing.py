"""Razorpay billing integration.

Setup:
  1. Create two products/plans in Razorpay Dashboard → Subscriptions:
       - KDP Checker Pro     (e.g. ₹1499/mo or $19/mo) → plan_id: plan_XXXX
       - KDP Checker Agency  (e.g. ₹5999/mo or $79/mo) → plan_id: plan_YYYY
  2. Copy your keys into .env:
       RAZORPAY_KEY_ID=rzp_test_...
       RAZORPAY_KEY_SECRET=...
       RAZORPAY_WEBHOOK_SECRET=...
       RAZORPAY_PRICE_PRO=plan_XXXX
       RAZORPAY_PRICE_AGENCY=plan_YYYY
  3. Point your webhook endpoint in Razorpay Dashboard to /billing/webhook and subscribe to:
       subscription.charged
       subscription.cancelled
       subscription.paused
"""
from __future__ import annotations

import os

import razorpay
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for, abort, jsonify
from flask_login import current_user, login_required

from kdp_checker import storage


billing_bp = Blueprint("billing", __name__, url_prefix="/billing")


PLANS = {
    "free": {"name": "Free", "price_cents": 0, "asin_limit": 1,
             "marketplaces": 13, "monitoring": False,
             "description": "1 ASIN, on-demand check"},
    "pro": {"name": "Pro", "price_cents": 1900, "asin_limit": 10,
            "marketplaces": 13, "monitoring": True,
            "description": "10 ASINs, daily monitoring, email alerts, support emails"},
    "agency": {"name": "Agency", "price_cents": 7900, "asin_limit": 100,
               "marketplaces": 13, "monitoring": True,
               "description": "100 ASINs, team seats, CSV exports, priority queue"},
}


def _configure_razorpay():
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return None
    return razorpay.Client(auth=(key_id, key_secret))


@billing_bp.get("/pricing")
def pricing():
    return render_template("pricing.html", plans=PLANS, user=current_user)


@billing_bp.post("/checkout/<plan>")
@login_required
def checkout(plan):
    VALID_PLANS = {
        "pro": os.getenv("RAZORPAY_PRICE_PRO"),
        "agency": os.getenv("RAZORPAY_PRICE_AGENCY")
    }

    if plan not in VALID_PLANS:
        abort(404)
        
    client = _configure_razorpay()
    if not client:
        return jsonify({"error": "Razorpay not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET."}), 503

    plan_id = VALID_PLANS[plan]
    if not plan_id:
        return jsonify({"error": f"RAZORPAY_PRICE_{plan.upper()} not set in environment"}), 503

    try:
        # Create a Subscription via Razorpay Subscriptions API
        # total_count=120 represents a 10-year monthly subscription
        sub_data = {
            "plan_id": plan_id,
            "total_count": 120,
            "quantity": 1,
            "customer_notify": 1,
            "notes": {
                "user_id": str(current_user.user_id),
                "plan": plan
            }
        }
        
        subscription = client.subscription.create(sub_data)
        short_url = subscription.get("short_url")
        if not short_url:
            return jsonify({"error": "Failed to generate checkout URL from Razorpay"}), 500
        
        return redirect(short_url, code=303)
    except Exception as e:
        return jsonify({"error": f"Razorpay error: {e}"}), 500


@billing_bp.get("/success")
@login_required
def success():
    return render_template("success.html")


@billing_bp.post("/webhook")
def webhook():
    client = _configure_razorpay()
    if not client:
        return "razorpay not configured", 503
        
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    payload = request.data
    signature = request.headers.get("X-Razorpay-Signature")
    
    if secret:
        try:
            # Verify Razorpay signature using the raw payload body bytes
            client.utility.verify_webhook_signature(
                payload,
                signature,
                secret
            )
        except Exception as e:
            current_app.logger.exception("Webhook verify failed")
            return f"bad signature: {e}", 400

    try:
        event_data = request.get_json(silent=True) or {}
    except Exception:
        return "invalid json", 400

    etype = event_data.get("event")
    payload_obj = event_data.get("payload", {})

    if etype in ("subscription.authenticated", "subscription.activated", "subscription.completed", "subscription.cancelled", "subscription.paused", "subscription.halted"):
        sub = payload_obj.get("subscription", {}).get("entity", {})
        sub_id = sub.get("id")
        customer_id = sub.get("customer_id")
        status = sub.get("status", "unknown")
        
        start_at = sub.get("current_start") or sub.get("start_at")
        end_at = sub.get("current_end") or sub.get("end_at")
        charge_at = sub.get("charge_at")
        last_payment = charge_at - 2592000 if charge_at else None
        
        notes = sub.get("notes", {})
        user_id = int(notes.get("user_id", 0) or 0)
        plan = notes.get("plan", "pro")
        
        # Robust fallback lookups
        if not user_id and customer_id:
            with storage.connect() as conn:
                user = conn.execute("SELECT id FROM users WHERE razorpay_customer_id = ?", (customer_id,)).fetchone()
                if user:
                    user_id = user["id"]
        if not user_id and sub_id:
            with storage.connect() as conn:
                user = conn.execute("SELECT id FROM users WHERE razorpay_subscription_id = ?", (sub_id,)).fetchone()
                if user:
                    user_id = user["id"]

        if user_id:
            db_plan = "free" if etype in ("subscription.cancelled", "subscription.paused", "subscription.halted") else plan
            with storage.connect() as conn:
                storage.update_user_plan(
                    conn,
                    user_id=user_id,
                    plan=db_plan,
                    razorpay_customer_id=customer_id,
                    razorpay_subscription_id=sub_id,
                    subscription_status=status,
                    subscription_start=start_at,
                    subscription_end=end_at,
                    last_payment_at=last_payment,
                    plan_type=plan
                )

    elif etype == "payment.failed":
        payment = payload_obj.get("payment", {}).get("entity", {})
        sub_id = payment.get("subscription_id")
        if sub_id:
            with storage.connect() as conn:
                user = conn.execute("SELECT id FROM users WHERE razorpay_subscription_id = ?", (sub_id,)).fetchone()
                if user:
                    storage.update_user_plan(
                        conn,
                        user_id=user["id"],
                        plan="free",
                        subscription_status="payment_failed",
                        plan_type="free"
                    )

    return "", 200


@billing_bp.post("/portal")
@login_required
def portal():
    # Graceful notification mapping to dashboard
    flash("To manage your Razorpay subscription, update cards, or cancel, please check your email invoice link or contact support.", "info")
    return redirect(url_for("dashboard"))
