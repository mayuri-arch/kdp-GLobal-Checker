"""Generate ready-to-send KDP support emails from detected issues.

We don't *send* mail — authors need to send from their KDP-registered address.
We produce clean subject + body pairs that they can paste into the KDP
"Contact Us" form or their email client.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .checker import CheckResult
from .detector import AvailabilityStatus
from .intelligence import Issue, IntelligenceReport


@dataclass
class EmailDraft:
    subject: str
    body: str
    recipient: str = "kdp-support@amazon.com"  # the user routes through the web form anyway
    category: str = "Technical"

    def to_dict(self) -> dict:
        return {"subject": self.subject, "body": self.body,
                "recipient": self.recipient, "category": self.category}


def _author_fill(text: str, author_name: str | None, book_title: str | None) -> str:
    name = author_name or "[YOUR NAME]"
    title = book_title or "[YOUR BOOK TITLE]"
    return text.replace("{AUTHOR}", name).replace("{TITLE}", title)


def draft_not_purchasable(
    asin: str,
    result: CheckResult,
    author_name: str | None = None,
    book_title: str | None = None,
) -> EmailDraft:
    m = result.marketplace
    a = result.analysis
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    signals = []
    if a.availability_text:
        signals.append(f'- Availability text on page: "{a.availability_text}"')
    signals.append(f"- Buy Now button present: {a.has_buy_button}")
    signals.append(f"- Add-to-Cart present: {a.has_add_to_cart}")
    signals.append(f"- Kindle Buy present: {a.has_kindle_buy}")
    signals.append(f"- Price shown: {a.price_text or 'none'}")

    body = _author_fill(f"""Hello KDP Support,

I am contacting you regarding my title "{{TITLE}}" (ASIN: {asin}) on amazon.{m.domain} ({m.country}).

The product page loads for customers, but the listing is not purchasable. Customers can see the page but cannot complete a purchase, which is causing direct revenue loss.

Detection summary (verified on {ts}):
- URL: {result.url}
- HTTP status: {a.http_status}
- Detected status: VISIBLE_NOT_PURCHASABLE
{chr(10).join(signals)}

KDP-side configuration I have already verified on my end:
- Territory / worldwide rights: ENABLED for {m.country}
- Book status in KDP Bookshelf: LIVE
- Price is set and within the {m.currency} royalty band
- No blocked categories or content warnings outstanding

Could you please:
1. Investigate why the Buy Box is missing on this marketplace.
2. Confirm whether there is a distribution, tax, or payment configuration issue on your side.
3. Restore purchasability as soon as possible.

Thank you,
{{AUTHOR}}
""", author_name, book_title)

    return EmailDraft(
        subject=f"[KDP] ASIN {asin} is not purchasable on amazon.{m.domain}",
        body=body.strip() + "\n",
        category="Technical issue — missing Buy Box",
    )


def draft_distribution_gap(
    asin: str,
    result: CheckResult,
    author_name: str | None = None,
    book_title: str | None = None,
) -> EmailDraft:
    m = result.marketplace
    body = _author_fill(f"""Hello KDP Support,

My title "{{TITLE}}" (ASIN: {asin}) is published on KDP with worldwide distribution enabled, but the product page on amazon.{m.domain} ({m.country}) returns a 'page not found' error.

URL tested: {result.url}
Detected status: NOT_FOUND

Could you please confirm whether this marketplace should be listing the title, and if so, restore the listing? If there is a rights/territory configuration I need to update on my side, please let me know the exact setting.

Thank you,
{{AUTHOR}}
""", author_name, book_title)
    return EmailDraft(
        subject=f"[KDP] ASIN {asin} missing on amazon.{m.domain}",
        body=body.strip() + "\n",
        category="Distribution",
    )


def generate_emails(
    asin: str,
    report: IntelligenceReport,
    results: list[CheckResult],
    author_name: str | None = None,
    book_title: str | None = None,
) -> list[EmailDraft]:
    """Generate one email per marketplace that requires author action."""
    by_code = {r.marketplace.code: r for r in results}
    drafts: list[EmailDraft] = []
    seen_codes = set()

    for issue in report.issues:
        mkt = issue.marketplace_code
        if not mkt or mkt in seen_codes:
            continue
        r = by_code.get(mkt)
        if r is None:
            continue
        if issue.code == "VISIBLE_NOT_PURCHASABLE":
            drafts.append(draft_not_purchasable(asin, r, author_name, book_title))
            seen_codes.add(mkt)
        elif issue.code == "DISTRIBUTION_GAP":
            drafts.append(draft_distribution_gap(asin, r, author_name, book_title))
            seen_codes.add(mkt)
    return drafts
