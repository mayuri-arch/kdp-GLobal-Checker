"""Stripe billing scaffolding.

Setup:
  1. Create two products in Stripe Dashboard:
       - KDP Checker Pro     (e.g. $19/mo)     → price_id: price_XXXX
       - KDP Checker Agency  (e.g. $79/mo)     → price_id: price_YYYY
  2. Copy your secret key + webhook signing secret into .env:
       STRIPE_SECRET_KEY=sk_live_...
       STRIPE_WEBHOOK_SECRET=whsec_...
       STRIPE_PRICE_PRO=price_XXXX
       STRIPE_PRICE_AGENCY=price_YYYY
  3. Point your webhook endpoint at /billing/webhook and subscribe to:
       checkout.session.completed
       customer.subscription.updated
       customer.subscription.deleted
"""
from __future__ import annotations

import os

import stripe
from flask import Blueprint, current_app, redirect, render_template, request, url_for, abort, jsonify
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


def _configure_stripe():
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return False
    stripe.api_key = key
    return True


@billing_bp.get("/pricing")
def pricing():
    return render_template("pricing.html", plans=PLANS, user=current_user)


@billing_bp.post("/checkout/<plan>")
@login_required
def checkout(plan):
    if plan not in PLANS or plan == "free":
        abort(404)
    if not _configure_stripe():
        return jsonify({"error": "Stripe not configured. Set STRIPE_SECRET_KEY."}), 503

    price_env = {"pro": "STRIPE_PRICE_PRO", "agency": "STRIPE_PRICE_AGENCY"}[plan]
    price_id = os.environ.get(price_env)
    if not price_id:
        return jsonify({"error": f"{price_env} not set in environment"}), 503

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=current_user.email,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=url_for("billing.success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("billing.pricing", _external=True),
        metadata={"user_id": str(current_user.user_id), "plan": plan},
        subscription_data={"metadata": {"user_id": str(current_user.user_id), "plan": plan}},
    )
    return redirect(session.url, code=303)


@billing_bp.get("/success")
@login_required
def success():
    return render_template("success.html")


@billing_bp.post("/webhook")
def webhook():
    if not _configure_stripe():
        return "stripe not configured", 503
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret) if secret \
                else stripe.Event.construct_from(request.get_json(silent=True) or {}, stripe.api_key)
    except Exception as e:
        current_app.logger.exception("Webhook verify failed")
        return f"bad signature: {e}", 400

    etype = event["type"]
    data = event["data"]["object"]

    if etype == "checkout.session.completed":
        user_id = int(data.get("metadata", {}).get("user_id", 0) or 0)
        plan = data.get("metadata", {}).get("plan", "pro")
        customer = data.get("customer")
        sub = data.get("subscription")
        if user_id:
            with storage.connect() as conn:
                storage.update_user_plan(conn, user_id, plan, customer, sub)

    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        user_id = int(data.get("metadata", {}).get("user_id", 0) or 0)
        if user_id:
            with storage.connect() as conn:
                storage.update_user_plan(conn, user_id, "free")

    return "", 200
