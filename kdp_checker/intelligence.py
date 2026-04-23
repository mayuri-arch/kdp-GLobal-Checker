"""Revenue intelligence engine.

Takes a list of CheckResult (one per marketplace) and returns a ranked list
of actionable issues. Each Issue has:
  - severity (critical / high / medium / low)
  - estimated_revenue_impact (0-100, relative)
  - recommendation (plain English, ready to show in UI or email)
  - fix_action (machine-readable: "contact_kdp", "adjust_price", etc.)

The priority score blends severity × marketplace weight (US/UK/DE pull harder
than BR/MX in absolute revenue for most KDP authors).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Iterable

from .checker import CheckResult
from .detector import AvailabilityStatus
from .pricing import price_band, to_usd


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FixAction(str, Enum):
    CONTACT_KDP = "contact_kdp"               # open support ticket
    ADJUST_PRICE = "adjust_price"             # change price in KDP
    ENABLE_MARKETPLACE = "enable_marketplace" # distribution gap
    INVESTIGATE_BLOCK = "investigate_block"   # manual verify
    IMPROVE_DESCRIPTION = "improve_description"
    REQUEST_REVIEWS = "request_reviews"
    MONITOR = "monitor"                       # historical watch


# Rough revenue weights by marketplace — normalized so US=100.
# Based on published KDP ebook unit-share estimates.
_MARKET_WEIGHT = {
    "US": 100, "UK": 35, "DE": 22, "CA": 10, "AU": 10,
    "IN": 8, "FR": 7, "IT": 6, "ES": 5, "NL": 4,
    "JP": 18, "BR": 4, "MX": 3,
}


@dataclass
class Issue:
    code: str                          # stable identifier
    title: str
    severity: Severity
    marketplace_code: str | None
    recommendation: str
    fix_action: FixAction
    estimated_revenue_impact: int      # 0-100 relative
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["fix_action"] = self.fix_action.value
        return d


@dataclass
class IntelligenceReport:
    asin: str
    summary: str
    live_count: int
    total: int
    issues: list[Issue]
    revenue_score: int   # 0-100, 100 = perfectly optimized globally

    def to_dict(self) -> dict:
        return {
            "asin": self.asin,
            "summary": self.summary,
            "live_count": self.live_count,
            "total": self.total,
            "revenue_score": self.revenue_score,
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------- Issue builders ----------

def _severity_weight(sev: Severity) -> int:
    return {Severity.CRITICAL: 100, Severity.HIGH: 70, Severity.MEDIUM: 45,
            Severity.LOW: 20, Severity.INFO: 5}[sev]


def _impact(severity: Severity, mkt_code: str | None) -> int:
    base = _severity_weight(severity)
    weight = _MARKET_WEIGHT.get(mkt_code or "", 20) / 100
    # Keep global-level issues (no specific market) at a moderate weight
    if mkt_code is None:
        weight = 0.5
    return max(1, min(100, int(round(base * (0.5 + weight)))))


def _issue_visible_not_purchasable(r: CheckResult) -> Issue:
    a = r.analysis
    return Issue(
        code="VISIBLE_NOT_PURCHASABLE",
        title=f"Book visible on {r.marketplace.country} but NOT purchasable",
        severity=Severity.CRITICAL,
        marketplace_code=r.marketplace.code,
        recommendation=(
            f"Your listing on amazon.{r.marketplace.domain} loads, but there is no buy "
            f"button or the product shows as unavailable. This is silent revenue loss — "
            f"every visitor converts to $0. Open a KDP support ticket with the ASIN and "
            f"marketplace; also verify territory rights and the 'Available' distribution "
            f"toggle in your KDP Bookshelf."
        ),
        fix_action=FixAction.CONTACT_KDP,
        estimated_revenue_impact=_impact(Severity.CRITICAL, r.marketplace.code),
        details={
            "availability_text": a.availability_text,
            "has_buy_button": a.has_buy_button,
            "has_add_to_cart": a.has_add_to_cart,
            "has_kindle_buy": a.has_kindle_buy,
            "price_text": a.price_text,
        },
    )


def _issue_not_found(r: CheckResult, authors_distributed: bool) -> Issue:
    # If most markets are live, a single NOT_FOUND usually means distribution gap.
    # If nothing is live anywhere, this is probably a wrong ASIN.
    if authors_distributed:
        return Issue(
            code="DISTRIBUTION_GAP",
            title=f"Book not distributed to {r.marketplace.country}",
            severity=Severity.HIGH,
            marketplace_code=r.marketplace.code,
            recommendation=(
                f"Your book does not appear on amazon.{r.marketplace.domain}. If this "
                f"marketplace should be enabled, go to KDP Bookshelf → edit your title → "
                f"Rights & Pricing, and ensure 'Worldwide rights' and all territories "
                f"are selected. Some territories (e.g. Brazil, Mexico) require explicit "
                f"opt-in."
            ),
            fix_action=FixAction.ENABLE_MARKETPLACE,
            estimated_revenue_impact=_impact(Severity.HIGH, r.marketplace.code),
            details={"http_status": r.analysis.http_status},
        )
    return Issue(
        code="ASIN_NOT_FOUND",
        title="ASIN not found on any marketplace",
        severity=Severity.CRITICAL,
        marketplace_code=r.marketplace.code,
        recommendation=(
            "The ASIN returns a 'not found' page across marketplaces. Double-check the "
            "ASIN in your KDP Bookshelf — it may be wrong, unpublished, or blocked."
        ),
        fix_action=FixAction.INVESTIGATE_BLOCK,
        estimated_revenue_impact=_impact(Severity.CRITICAL, r.marketplace.code),
    )


def _issue_blocked(r: CheckResult) -> Issue:
    return Issue(
        code="BLOCKED_UNKNOWN",
        title=f"Could not verify {r.marketplace.country} (anti-bot wall)",
        severity=Severity.MEDIUM,
        marketplace_code=r.marketplace.code,
        recommendation=(
            "Amazon returned a captcha / robot check for this marketplace, so we cannot "
            "confirm purchasability. Retry later, or open the URL manually to verify. "
            "In production, run this check behind a residential proxy pool."
        ),
        fix_action=FixAction.INVESTIGATE_BLOCK,
        estimated_revenue_impact=_impact(Severity.MEDIUM, r.marketplace.code),
    )


def _issue_price_anomaly(r: CheckResult, median_usd: float | None) -> Issue | None:
    a = r.analysis
    if a.price_value is None:
        return None
    band = price_band(r.marketplace.code)
    if band is None:
        return None

    # 1. Price outside 70% royalty band
    if not band.in_70pct_band(a.price_value):
        direction = "too high" if a.price_value > band.max_70pct else "too low"
        return Issue(
            code="PRICE_OUT_OF_70PCT_BAND",
            title=f"Price {direction} for 70% royalty in {r.marketplace.country}",
            severity=Severity.HIGH,
            marketplace_code=r.marketplace.code,
            recommendation=(
                f"Current price: {a.price_text}. KDP's 70% royalty band in "
                f"{r.marketplace.country} is {band.min_70pct}-{band.max_70pct} "
                f"{band.currency}. Outside this band you drop to 35% royalty, "
                f"halving your per-unit revenue. Sweet spot: "
                f"{band.sweet_low}-{band.sweet_high} {band.currency}."
            ),
            fix_action=FixAction.ADJUST_PRICE,
            estimated_revenue_impact=_impact(Severity.HIGH, r.marketplace.code),
            details={"price": a.price_value, "currency": band.currency,
                     "band_min": band.min_70pct, "band_max": band.max_70pct},
        )

    # 2. Price radically deviates from cross-marketplace median (USD-adjusted)
    usd = to_usd(r.marketplace.code, a.price_value)
    if median_usd and usd and median_usd > 0:
        ratio = usd / median_usd
        if ratio >= 1.6 or ratio <= 0.4:
            direction = "too high" if ratio >= 1.6 else "too low"
            return Issue(
                code="PRICE_DEVIATES_FROM_MEDIAN",
                title=f"Price {direction} vs. other markets in {r.marketplace.country}",
                severity=Severity.MEDIUM,
                marketplace_code=r.marketplace.code,
                recommendation=(
                    f"Price in {r.marketplace.country} converts to ~${usd} USD, vs. a "
                    f"median of ~${round(median_usd, 2)} across your live markets. "
                    f"Large deviations often indicate a mis-set price or missed PPP "
                    f"opportunity."
                ),
                fix_action=FixAction.ADJUST_PRICE,
                estimated_revenue_impact=_impact(Severity.MEDIUM, r.marketplace.code),
                details={"usd": usd, "median_usd": round(median_usd, 2), "ratio": round(ratio, 2)},
            )
    return None


def _issue_low_conversion(r: CheckResult) -> Issue | None:
    a = r.analysis
    weak_bits = []
    if (a.review_count or 0) < 5:
        weak_bits.append("fewer than 5 ratings")
    if not a.has_description:
        weak_bits.append("thin or missing description")
    if a.rating is not None and a.rating < 3.5:
        weak_bits.append(f"rating {a.rating} is below 3.5")
    if not weak_bits:
        return None

    sev = Severity.HIGH if len(weak_bits) >= 2 else Severity.MEDIUM
    fix = FixAction.REQUEST_REVIEWS if "ratings" in " ".join(weak_bits) else FixAction.IMPROVE_DESCRIPTION
    return Issue(
        code="LOW_CONVERSION_SIGNALS",
        title=f"Weak conversion signals in {r.marketplace.country}",
        severity=sev,
        marketplace_code=r.marketplace.code,
        recommendation=(
            f"Your page is purchasable but visitors have weak reasons to buy: "
            f"{', '.join(weak_bits)}. Seed reviews via your newsletter / ARC readers, "
            f"and rewrite the A+ description to lead with the transformation the reader "
            f"gets in the first 2 lines."
        ),
        fix_action=fix,
        estimated_revenue_impact=_impact(sev, r.marketplace.code),
        details={"review_count": a.review_count, "rating": a.rating,
                 "has_description": a.has_description},
    )


# ---------- Main entry point ----------

def analyze_results(asin: str, results: Iterable[CheckResult]) -> IntelligenceReport:
    results = list(results)
    total = len(results)
    live = sum(1 for r in results if r.analysis.status.is_live)
    authors_distributed = live > 0

    # Compute USD median across live markets for cross-market comparison
    usds = []
    for r in results:
        if r.analysis.price_value and r.analysis.status.is_live:
            u = to_usd(r.marketplace.code, r.analysis.price_value)
            if u is not None:
                usds.append(u)
    median_usd = statistics.median(usds) if usds else None

    issues: list[Issue] = []
    for r in results:
        s = r.analysis.status
        if s == AvailabilityStatus.VISIBLE_NOT_PURCHASABLE:
            issues.append(_issue_visible_not_purchasable(r))
        elif s == AvailabilityStatus.NOT_FOUND:
            issues.append(_issue_not_found(r, authors_distributed))
        elif s == AvailabilityStatus.BLOCKED:
            issues.append(_issue_blocked(r))
        elif s == AvailabilityStatus.RESTRICTED:
            issues.append(Issue(
                code="RESTRICTED_REGION",
                title=f"{r.marketplace.country} restricts this listing",
                severity=Severity.LOW, marketplace_code=r.marketplace.code,
                recommendation="Some regions block specific categories. Verify category/territory in KDP.",
                fix_action=FixAction.MONITOR,
                estimated_revenue_impact=_impact(Severity.LOW, r.marketplace.code),
            ))
        elif s.is_live:
            pi = _issue_price_anomaly(r, median_usd)
            if pi: issues.append(pi)
            ci = _issue_low_conversion(r)
            if ci: issues.append(ci)

    # Sort by (severity, impact desc)
    sev_rank = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2,
                Severity.LOW: 3, Severity.INFO: 4}
    issues.sort(key=lambda i: (sev_rank[i.severity], -i.estimated_revenue_impact))

    # Revenue score: weighted % of available revenue captured.
    total_weight = sum(_MARKET_WEIGHT.get(r.marketplace.code, 20) for r in results)
    captured = 0
    for r in results:
        w = _MARKET_WEIGHT.get(r.marketplace.code, 20)
        if r.analysis.status == AvailabilityStatus.LIVE_OPTIMIZED:
            captured += w
        elif r.analysis.status == AvailabilityStatus.LIVE_LOW_CONVERSION:
            captured += int(w * 0.6)  # purchasable but leaking conversion
        elif r.analysis.status == AvailabilityStatus.BLOCKED:
            captured += int(w * 0.5)  # unknown, don't punish fully
    revenue_score = int(round(100 * captured / total_weight)) if total_weight else 0

    summary = f"{live}/{total} marketplaces LIVE · revenue score {revenue_score}/100"
    critical_mkts = [i.marketplace_code for i in issues
                     if i.severity == Severity.CRITICAL and i.marketplace_code]
    if critical_mkts:
        summary += f" · CRITICAL: {', '.join(critical_mkts)}"

    return IntelligenceReport(
        asin=asin, summary=summary, live_count=live, total=total,
        issues=issues, revenue_score=revenue_score,
    )
