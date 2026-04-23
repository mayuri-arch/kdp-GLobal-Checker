"""Page analysis v2 — classifies an Amazon product page with purchasability signals.

Philosophy: "visible" is not the same as "purchasable". A book can have a live
page but no buy button, which is the single highest-cost failure mode for KDP
authors. We detect this explicitly and split LIVE into two tiers based on
conversion signals (reviews, description, ratings).

Status ladder (worst → best):
    ERROR → BLOCKED → NOT_FOUND → RESTRICTED → VISIBLE_NOT_PURCHASABLE →
    LIVE_LOW_CONVERSION → LIVE_OPTIMIZED
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from enum import Enum

from bs4 import BeautifulSoup

from .marketplaces import Marketplace


class AvailabilityStatus(str, Enum):
    LIVE_OPTIMIZED = "LIVE_OPTIMIZED"                # purchasable + strong conversion signals
    LIVE_LOW_CONVERSION = "LIVE_LOW_CONVERSION"      # purchasable but weak signals (few reviews, thin desc)
    VISIBLE_NOT_PURCHASABLE = "VISIBLE_NOT_PURCHASABLE"  # page exists, no buy button or "unavailable"
    NOT_FOUND = "NOT_FOUND"                          # 404 / not distributed here
    BLOCKED = "BLOCKED"                              # captcha / anti-bot — unknown, retry w/ browser
    RESTRICTED = "RESTRICTED"                        # marketplace not serving this region
    ERROR = "ERROR"                                  # transport / parse error

    @property
    def is_live(self) -> bool:
        return self in (self.LIVE_OPTIMIZED, self.LIVE_LOW_CONVERSION)

    @property
    def label(self) -> str:
        return {
            "LIVE_OPTIMIZED": "[LIVE+]",
            "LIVE_LOW_CONVERSION": "[LIVE-]",
            "VISIBLE_NOT_PURCHASABLE": "[UNBUY]",
            "NOT_FOUND": "[404]",
            "BLOCKED": "[BLK]",
            "RESTRICTED": "[RST]",
            "ERROR": "[ERR]",
        }[self.value]


@dataclass
class PageAnalysis:
    status: AvailabilityStatus
    http_status: int | None = None
    final_url: str | None = None
    title: str | None = None
    author: str | None = None
    price_text: str | None = None      # raw "$9.99"
    price_value: float | None = None   # parsed 9.99
    currency: str | None = None
    has_buy_button: bool = False
    has_add_to_cart: bool = False
    has_kindle_buy: bool = False
    availability_text: str | None = None
    review_count: int | None = None
    rating: float | None = None
    has_description: bool = False
    bestseller_rank: str | None = None
    asin_confirmed: bool = False
    reason: str = ""
    error: str | None = None
    signals: list[str] = field(default_factory=list)  # human-readable breadcrumbs
    source: str = "http"  # "http" or "browser"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ---------- Detection signal vocabularies ----------

_CAPTCHA_SIGNALS = (
    "/errors/validateCaptcha",
    "Enter the characters you see below",
    "To discuss automated access to Amazon data",
    "api-services-support@amazon.com",
    "Type the characters you see in this image",
    "Robot Check",
)
_RESTRICTED_SIGNALS = (
    "is not available in your country",
    "not shipping to your location",
    "This app isn't available in your country",
)

# Buy-button DOM signatures (stable IDs Amazon has used for years)
_BUY_NOW_SELECTORS = (
    "#buy-now-button",
    "#buyNowButton",
    'input[name="submit.buy-now"]',
    'input[id*="buy-now"]',
    "#one-click-button",
    'input[name="submit.buy-oneclick"]',
)
_ADD_TO_CART_SELECTORS = (
    "#add-to-cart-button",
    'input[name="submit.add-to-cart"]',
    'input[id*="add-to-cart"]',
)
_KINDLE_BUY_SELECTORS = (
    "#ebooksInstantOrderUpdate",
    "#ebooksProductTitle",
    'input[name="submit.buy-now-kindle"]',
    'a[id="one-click-button"]',
)

# Price selectors — ordered from most-specific to most-generic
_PRICE_SELECTORS = (
    "#kindle-price",
    "#tmm-grid-swatch-KINDLE .slot-price span",
    "#tmm-grid-swatch-PAPERBACK .slot-price span",
    "#tmm-grid-swatch-HARDCOVER .slot-price span",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    ".a-button-selected .a-color-price",
    "#price",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    ".a-price .a-offscreen",
)

_PRICE_NUM_RE = re.compile(r"([\d]+[\d.,]*)")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _parse_price_value(text: str) -> float | None:
    """Extract a float from locale-formatted price strings like '₹299,00' or '$9.99'."""
    if not text:
        return None
    m = _PRICE_NUM_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    # Heuristic: if both , and . present, the LAST one is the decimal separator
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # ,XX at end with 2 digits → decimal (European). Otherwise thousands.
        if re.search(r",\d{2}$", raw):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _extract_title(soup: BeautifulSoup) -> str | None:
    for sel in ("#productTitle", "#title span", "span#ebooksProductTitle", "h1#title"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return _clean(el.get_text())
    if soup.title and soup.title.string:
        t = _clean(soup.title.string)
        parts = re.split(r"\s*:\s*", t)
        if len(parts) >= 2 and "amazon" in parts[0].lower():
            return parts[1] if parts[1] else t
        return t
    return None


def _extract_author(soup: BeautifulSoup) -> str | None:
    for sel in ("#bylineInfo .author a", ".author .contributorNameID",
                "#bylineInfo span.author a", "a.contributorNameID"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return _clean(el.get_text())
    return None


def _extract_price(soup: BeautifulSoup) -> tuple[str | None, float | None]:
    for sel in _PRICE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            txt = _clean(el.get_text())
            if txt and any(c.isdigit() for c in txt):
                return txt, _parse_price_value(txt)
    return None, None


def _extract_reviews(soup: BeautifulSoup) -> tuple[int | None, float | None]:
    count = None
    rating = None
    # Review count: "1,234 ratings" / "12 reviews"
    el = soup.select_one("#acrCustomerReviewText")
    if el:
        m = re.search(r"([\d.,]+)", el.get_text())
        if m:
            try:
                count = int(m.group(1).replace(",", "").replace(".", ""))
            except ValueError:
                pass
    el = soup.select_one("span.a-icon-alt, i.a-icon-star span")
    if el:
        m = re.search(r"([\d]+[.,]?[\d]*)", el.get_text())
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return count, rating


def _extract_bestseller_rank(soup: BeautifulSoup) -> str | None:
    el = soup.select_one("#SalesRank")
    if el:
        return _clean(el.get_text())[:200]
    # Fallback: scan detail bullets
    for li in soup.select("#detailBulletsWrapper_feature_div li, #productDetailsTable li"):
        txt = li.get_text(" ", strip=True)
        if "Best Sellers Rank" in txt or "Amazon Bestseller" in txt:
            return _clean(txt)[:200]
    return None


def _availability_text(soup: BeautifulSoup) -> str | None:
    for sel in ("#availability .a-color-success",
                "#availability .a-color-price",
                "#availability span",
                "#outOfStock"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return _clean(el.get_text())
    return None


def _first_selector_hit(soup: BeautifulSoup, selectors) -> bool:
    for sel in selectors:
        try:
            if soup.select_one(sel):
                return True
        except Exception:
            continue
    return False


# ---------- Public API ----------

def analyze_page(
    marketplace: Marketplace,
    html: str,
    http_status: int,
    final_url: str,
    asin: str,
    source: str = "http",
) -> PageAnalysis:
    # --- transport-level first ---
    if http_status == 404:
        return PageAnalysis(status=AvailabilityStatus.NOT_FOUND, http_status=http_status,
                            final_url=final_url, reason="HTTP 404", source=source)
    if http_status >= 500:
        return PageAnalysis(status=AvailabilityStatus.ERROR, http_status=http_status,
                            final_url=final_url, reason=f"HTTP {http_status}", source=source)
    if "/errors/" in (final_url or "") and "validateCaptcha" not in (final_url or ""):
        return PageAnalysis(status=AvailabilityStatus.NOT_FOUND, http_status=http_status,
                            final_url=final_url, reason="Redirected to error page", source=source)

    # --- anti-bot ---
    for sig in _CAPTCHA_SIGNALS:
        if sig in html:
            return PageAnalysis(status=AvailabilityStatus.BLOCKED, http_status=http_status,
                                final_url=final_url, reason="Anti-bot / captcha wall", source=source)
    for sig in _RESTRICTED_SIGNALS:
        if sig in html:
            return PageAnalysis(status=AvailabilityStatus.RESTRICTED, http_status=http_status,
                                final_url=final_url, reason="Marketplace restriction", source=source)

    soup = BeautifulSoup(html, "lxml")
    html_lower_hit = lambda phrases: next((p for p in phrases if p.lower() in html.lower()), None)

    not_found_hit = html_lower_hit(marketplace.not_found_phrases)
    has_product_dom = bool(
        soup.select_one("#productTitle")
        or soup.select_one("#dp-container")
        or soup.select_one("#ppd")
        or soup.select_one("#centerCol")
        or soup.select_one("#dp")
    )
    asin_confirmed = asin.upper() in html.upper()

    if not_found_hit and not has_product_dom:
        return PageAnalysis(status=AvailabilityStatus.NOT_FOUND, http_status=http_status,
                            final_url=final_url, reason=f'Matched: "{not_found_hit}"', source=source)
    if not has_product_dom:
        return PageAnalysis(status=AvailabilityStatus.NOT_FOUND, http_status=http_status,
                            final_url=final_url, reason="No product DOM found",
                            asin_confirmed=asin_confirmed, source=source)

    # --- product page — now measure purchasability and conversion ---
    title = _extract_title(soup)
    author = _extract_author(soup)
    price_text, price_value = _extract_price(soup)
    review_count, rating = _extract_reviews(soup)
    bsr = _extract_bestseller_rank(soup)
    avail_text = _availability_text(soup)
    description_el = (soup.select_one("#bookDescription_feature_div")
                      or soup.select_one("#productDescription")
                      or soup.select_one("#feature-bullets"))
    has_description = bool(description_el and len(description_el.get_text(strip=True)) > 80)

    has_buy = _first_selector_hit(soup, _BUY_NOW_SELECTORS)
    has_cart = _first_selector_hit(soup, _ADD_TO_CART_SELECTORS)
    has_kindle = _first_selector_hit(soup, _KINDLE_BUY_SELECTORS)

    signals: list[str] = []
    if title: signals.append("title")
    if price_text: signals.append(f"price={price_text}")
    if has_buy: signals.append("buy-now")
    if has_cart: signals.append("add-to-cart")
    if has_kindle: signals.append("kindle-buy")
    if review_count: signals.append(f"reviews={review_count}")
    if rating: signals.append(f"rating={rating}")
    if has_description: signals.append("desc")
    if bsr: signals.append("bsr")

    # "Currently unavailable" phrase from locale pack, or empty availability with no price/buy
    unavail_phrase = html_lower_hit(marketplace.unavailable_phrases)
    purchasable = (has_buy or has_cart or has_kindle) and bool(price_text)

    base = PageAnalysis(
        status=AvailabilityStatus.VISIBLE_NOT_PURCHASABLE,  # placeholder, set below
        http_status=http_status,
        final_url=final_url,
        title=title,
        author=author,
        price_text=price_text,
        price_value=price_value,
        currency=marketplace.currency,
        has_buy_button=has_buy,
        has_add_to_cart=has_cart,
        has_kindle_buy=has_kindle,
        availability_text=avail_text,
        review_count=review_count,
        rating=rating,
        has_description=has_description,
        bestseller_rank=bsr,
        asin_confirmed=asin_confirmed,
        signals=signals,
        source=source,
    )

    if unavail_phrase or not purchasable:
        base.status = AvailabilityStatus.VISIBLE_NOT_PURCHASABLE
        base.reason = (f'Unavailable: "{unavail_phrase}"' if unavail_phrase
                       else "No buy button / price missing")
        return base

    # Purchasable — split by conversion strength
    strong = (
        (review_count or 0) >= 5
        and has_description
        and (rating or 0) >= 3.5
    )
    if strong:
        base.status = AvailabilityStatus.LIVE_OPTIMIZED
        base.reason = "Purchasable with strong conversion signals"
    else:
        base.status = AvailabilityStatus.LIVE_LOW_CONVERSION
        weak = []
        if (review_count or 0) < 5: weak.append("few/no reviews")
        if not has_description: weak.append("thin description")
        if rating is not None and rating < 3.5: weak.append(f"low rating {rating}")
        base.reason = "Purchasable but weak signals: " + ", ".join(weak or ["unknown"])
    return base
