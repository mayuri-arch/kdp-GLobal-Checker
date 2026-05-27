"""SMTP-based email notification engine for availability status flips.

Scans the `change_events` table for unnotified changes, consolidates them by user
to avoid spamming their inbox, and sends a single, high-trust digest email alert.
"""
from __future__ import annotations

import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from . import storage
from .marketplaces import MARKETPLACES_BY_CODE

log = logging.getLogger("kdp.notifier")


def _get_smtp_config() -> dict[str, str | int | None]:
    """Retrieve SMTP credentials from the active environment variables."""
    return {
        "host": os.environ.get("SMTP_HOST"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER"),
        "password": os.environ.get("SMTP_PASSWORD"),
        "from_addr": os.environ.get("SMTP_FROM", "noreply@kdpchecker.com"),
    }


def send_pending_notifications(conn) -> int:
    """Scan and transmit email alerts to users for any unnotified change events."""
    try:
        pending = list(storage.pending_notifications(conn))
    except Exception:
        log.exception("Failed to query pending notifications from database.")
        return 0

    if not pending:
        return 0

    # Group pending events by user email to send a consolidated digest
    user_digests: dict[str, list[dict]] = {}
    for row in pending:
        email = row["email"]
        if not email:
            continue
        user_digests.setdefault(email, []).append(dict(row))

    config = _get_smtp_config()
    sent_count = 0
    processed_ids = []

    for email, events in user_digests.items():
        try:
            subject = "[KDP Alert] Availability changes detected for your books"
            body_text, body_html = _build_digest_content(email, events)

            if config["host"]:
                # Real SMTP delivery
                _send_email(
                    to_addr=email,
                    subject=subject,
                    text=body_text,
                    html=body_html,
                    config=config
                )
                log.info("Sent change alert email to %s for %d event(s)", email, len(events))
            else:
                # Stderr / stdout fallback for local development environment
                log.info("Local SMTP not configured. Writing digest email to log for %s:", email)
                divider = "=" * 80
                sys.stdout.write(
                    f"\n{divider}\n"
                    f"FROM: {config['from_addr']}\n"
                    f"TO: {email}\n"
                    f"SUBJECT: {subject}\n"
                    f"{divider}\n"
                    f"{body_text}\n"
                    f"{divider}\n\n"
                )
                sys.stdout.flush()

            # Record successfully processed IDs
            processed_ids.extend([e["id"] for e in events])
            sent_count += 1
        except Exception:
            log.exception("Failed to transmit email notification to %s", email)

    if processed_ids:
        try:
            storage.mark_notified(conn, processed_ids)
            log.info("Marked %d change events as notified in database.", len(processed_ids))
        except Exception:
            log.exception("Failed to mark change events as notified.")

    return sent_count


def _build_digest_content(email: str, events: list[dict]) -> tuple[str, str]:
    """Format the digest email body in plain-text and HTML versions."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Plain text version
    text_lines = [
        "Hello KDP Publisher,",
        "",
        "We detected availability or purchasability changes for your monitored books on Amazon.",
        "Please inspect the details below to protect your KDP royalty revenues:",
        "",
        "-" * 70
    ]

    # HTML version
    html_rows = []

    for e in events:
        asin = e["asin"]
        mkt_code = e["marketplace_code"]
        mkt = MARKETPLACES_BY_CODE.get(mkt_code)
        mkt_name = mkt.country if mkt else mkt_code
        domain = mkt.domain if mkt else mkt_code.lower()
        url = f"https://www.amazon.{domain}/dp/{asin}"

        from_status = (e["from_status"] or "NOT_FOUND").replace("_", " ")
        to_status = e["to_status"].replace("_", " ")
        detected_at = datetime.fromtimestamp(e["detected_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # Color codes based on status severity
        color = "#10b981" if "OPTIMIZED" in to_status else ("#f59e0b" if "LOW_CONVERSION" in to_status else "#ef4444")

        # Plain text row
        text_lines.extend([
            f"ASIN: {asin}",
            f"Marketplace: {mkt_name} (amazon.{domain})",
            f"Change: {from_status}  ===>  {to_status}",
            f"Detected At: {detected_at}",
            f"Link: {url}",
            "-" * 70
        ])

        # HTML row
        html_rows.append(f"""
        <tr>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-family: sans-serif; font-size: 14px;">
                <strong>{asin}</strong>
            </td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-family: sans-serif; font-size: 14px;">
                {mkt_name} (<a href="{url}" style="color: #3b82f6; text-decoration: none;">amazon.{domain}</a>)
            </td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-family: sans-serif; font-size: 14px;">
                <span style="color: #64748b; font-size: 12px;">{from_status}</span> 
                <span style="color: #94a3b8;">&rarr;</span> 
                <span style="color: {color}; font-weight: bold;">{to_status}</span>
            </td>
            <td style="padding: 12px; border-bottom: 1px solid #e2e8f0; font-family: sans-serif; font-size: 14px; color: #64748b;">
                {detected_at}
            </td>
        </tr>
        """)

    text_lines.extend([
        "",
        "To manage your monitored ASINs, view full reports, or retrieve KDP support email drafts,",
        "please log in to your Publisher Dashboard.",
        "",
        "Thank you,",
        "KDP Global Checker Team",
        f"(Sent automatically at {now_str})"
    ])

    html_content = f"""
    <html>
    <body style="background-color: #f8fafc; padding: 24px; font-family: sans-serif; color: #1e293b;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="600" style="background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0;">
            <tr>
                <td style="background-color: #0f172a; padding: 24px; text-align: center;">
                    <h1 style="color: #ffffff; margin: 0; font-size: 20px; font-weight: bold; letter-spacing: -0.5px;">KDP Global Checker Alert</h1>
                </td>
            </tr>
            <tr>
                <td style="padding: 24px;">
                    <p style="margin: 0 0 16px 0; font-size: 15px; line-height: 1.5;">Hello KDP Publisher,</p>
                    <p style="margin: 0 0 20px 0; font-size: 15px; line-height: 1.5; color: #475569;">
                        We detected availability or purchasability changes for your monitored books on Amazon. Please inspect the details below to protect your KDP royalty revenues:
                    </p>
                    
                    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin-bottom: 24px;">
                        <thead>
                            <tr style="background-color: #f1f5f9;">
                                <th align="left" style="padding: 10px 12px; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1;">ASIN</th>
                                <th align="left" style="padding: 10px 12px; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1;">Storefront</th>
                                <th align="left" style="padding: 10px 12px; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1;">Availability Shift</th>
                                <th align="left" style="padding: 10px 12px; color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #cbd5e1;">Time</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(html_rows)}
                        </tbody>
                    </table>

                    <div style="background-color: #f1f5f9; border-radius: 8px; padding: 16px; text-align: center; margin-bottom: 20px;">
                        <p style="margin: 0 0 12px 0; font-size: 14px; color: #475569; font-weight: 500;">Need to contact KDP Support?</p>
                        <p style="margin: 0; font-size: 13px; color: #64748b; line-height: 1.45;">
                            Log in to retrieve auto-generated "Ready-to-Send" support emails pre-filled with this detection evidence.
                        </p>
                    </div>
                </td>
            </tr>
            <tr>
                <td style="background-color: #f8fafc; padding: 16px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
                    KDP Global Checker Team &bull; Guarding Your Royalties 24/7<br>
                    <span style="font-size: 11px; color: #cbd5e1; margin-top: 4px; display: inline-block;">Sent automatically at {now_str}</span>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    return "\n".join(text_lines), html_content.strip()


def _send_email(to_addr: str, subject: str, text: str, html: str, config: dict) -> None:
    """Initiate an SMTP connection and deliver the email message."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["from_addr"]
    msg["To"] = to_addr

    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    # Establish connection
    smtp = smtplib.SMTP(config["host"], config["port"], timeout=15)
    try:
        if config["port"] == 587:
            smtp.starttls()
        if config["user"] and config["password"]:
            smtp.login(config["user"], config["password"])
        smtp.sendmail(config["from_addr"], [to_addr], msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
