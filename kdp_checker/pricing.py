"""Per-marketplace pricing reference.

Source: KDP 70% royalty eligibility bands + PPP-adjusted sweet spots.
These are *guidance bands*, not hard rules — the intelligence engine uses
them to flag anomalies, not to mandate a specific price.

Every band is in the marketplace's LOCAL currency.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceBand:
    currency: str
    min_70pct: float       # lower bound for 70% royalty (ebook)
    max_70pct: float       # upper bound for 70% royalty (ebook)
    sweet_low: float       # start of high-converting band
    sweet_high: float      # end of high-converting band
    # Rough USD purchasing-power equivalent for sanity-checking cross-market price
    usd_rate: float        # local_price / usd_rate ≈ USD

    def in_70pct_band(self, price: float) -> bool:
        return self.min_70pct <= price <= self.max_70pct

    def in_sweet_spot(self, price: float) -> bool:
        return self.sweet_low <= price <= self.sweet_high


# Conservative reference values. Update periodically as KDP bands shift.
PRICE_BANDS: dict[str, PriceBand] = {
    "US": PriceBand("USD",  2.99,  9.99,  2.99,  6.99,  1.00),
    "UK": PriceBand("GBP",  1.77,  7.81,  2.49,  5.99,  0.79),
    "CA": PriceBand("CAD",  2.99,  9.99,  3.99,  7.99,  1.37),
    "AU": PriceBand("AUD",  3.99, 11.99,  4.99,  9.99,  1.52),
    "DE": PriceBand("EUR",  2.99,  9.99,  2.99,  6.99,  0.92),
    "FR": PriceBand("EUR",  2.99,  9.99,  2.99,  6.99,  0.92),
    "IT": PriceBand("EUR",  2.99,  9.99,  2.99,  6.99,  0.92),
    "ES": PriceBand("EUR",  2.99,  9.99,  2.99,  6.99,  0.92),
    "NL": PriceBand("EUR",  2.99,  9.99,  2.99,  6.99,  0.92),
    "JP": PriceBand("JPY",  250,   1250,  299,    980,   150.0),
    "IN": PriceBand("INR",   49,   650,    99,    399,    84.0),
    "BR": PriceBand("BRL",  5.99, 20.00,  9.99, 14.99,   5.10),
    "MX": PriceBand("MXN", 35.00,199.00, 69.00,129.00,  18.00),
}


def price_band(code: str) -> PriceBand | None:
    return PRICE_BANDS.get(code.upper())


def to_usd(code: str, local_price: float) -> float | None:
    pb = price_band(code)
    if pb is None or local_price is None:
        return None
    return round(local_price / pb.usd_rate, 2)
