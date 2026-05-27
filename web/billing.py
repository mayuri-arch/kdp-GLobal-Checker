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
    currency_symbol = os.environ.get("BILLING_CURRENCY_SYMBOL", "$")
    return render_template("pricing.html", plans=PLANS, user=current_user, currency_symbol=currency_symbol)


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
    sub_id = request.args.get("razorpay_subscription_id")
    if sub_id:
        client = _configure_razorpay()
        if client:
            try:
                # Synchronously verify subscription status and perform instant upgrade
                subscription = client.subscription.fetch(sub_id)
                status = subscription.get("status")
                notes = subscription.get("notes", {})
                plan = notes.get("plan", "pro")
                customer_id = subscription.get("customer_id")
                
                if status in ("active", "authenticated", "activated"):
                    with storage.connect() as conn:
                        storage.update_user_plan(
                            conn,
                            user_id=current_user.user_id,
                            plan=plan,
                            razorpay_customer_id=customer_id,
                            razorpay_subscription_id=sub_id,
                            subscription_status=status,
                            subscription_start=subscription.get("current_start") or subscription.get("start_at"),
                            subscription_end=subscription.get("current_end") or subscription.get("end_at"),
                            plan_type=plan
                        )
                    # Dynamically update the current session to avoid requiring page refresh
                    current_user.plan = plan
                    flash(f"Success! Your plan has been upgraded to {plan.upper()}.", "success")
            except Exception as e:
                current_app.logger.exception(f"Synchronous subscription check failed: {e}")

    return render_template("success.html")


@billing_bp.post("/success")
@login_required
def success_post():
    sub_id = (request.form.get("subscription_id") or "").strip()
    if not sub_id or not sub_id.startswith("sub_"):
        flash("Invalid Subscription ID format. Should start with 'sub_'.", "error")
        return redirect(url_for("billing.success"))
        
    client = _configure_razorpay()
    if not client:
        flash("Razorpay is not configured.", "error")
        return redirect(url_for("billing.success"))
        
    try:
        # Securely fetch subscription status directly from Razorpay API
        subscription = client.subscription.fetch(sub_id)
        status = subscription.get("status")
        notes = subscription.get("notes", {})
        plan = notes.get("plan", "pro")
        customer_id = subscription.get("customer_id")
        
        if status in ("active", "authenticated", "activated"):
            with storage.connect() as conn:
                storage.update_user_plan(
                    conn,
                    user_id=current_user.user_id,
                    plan=plan,
                    razorpay_customer_id=customer_id,
                    razorpay_subscription_id=sub_id,
                    subscription_status=status,
                    subscription_start=subscription.get("current_start") or subscription.get("start_at"),
                    subscription_end=subscription.get("current_end") or subscription.get("end_at"),
                    plan_type=plan
                )
            current_user.plan = plan
            flash(f"Success! Subscription verified. Account upgraded to {plan.upper()}.", "success")
            return redirect(url_for("dashboard"))
        else:
            flash(f"Subscription found but status is '{status}'. It must be active to upgrade.", "error")
    except Exception as e:
        flash(f"Verification failed: {e}", "error")
        
    return redirect(url_for("billing.success"))


@billing_bp.post("/webhook")
def webhook():
    client = _configure_razorpay()
    if not client:
        return "razorpay not configured", 503
        
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    payload = request.data
    payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    signature = request.headers.get("X-Razorpay-Signature")
    
    if secret:
        try:
            # Verify Razorpay signature using the decoded payload string to prevent SDK TypeErrors
            client.utility.verify_webhook_signature(
                payload_str,
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
            
            # If the user cancels their subscription, automatically issue a full refund for their last active charge
            if etype == "subscription.cancelled":
                try:
                    payments_obj = client.subscription.payments(sub_id)
                    items = payments_obj.get("items", [])
                    if items:
                        # Find the most recent successful/captured payment
                        captured_payments = [p for p in items if p.get("status") == "captured"]
                        if captured_payments:
                            last_payment = captured_payments[0]
                            payment_id = last_payment.get("id")
                            amount = last_payment.get("amount")
                            
                            if payment_id and amount:
                                client.payment.refund(payment_id, {
                                    "amount": amount,
                                    "notes": {
                                        "reason": "Automatic subscription cancellation refund"
                                    }
                                })
                                current_app.logger.info(f"Auto-refunded payment {payment_id} for subscription {sub_id} successfully.")
                except Exception as refund_err:
                    current_app.logger.error(f"Automatic refund failed for subscription {sub_id}: {refund_err}")

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
