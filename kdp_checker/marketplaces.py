"""Amazon marketplace registry with locale-specific signals for detection."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Marketplace:
    code: str              # ISO country code
    country: str           # Human-readable name
    domain: str            # amazon.{domain}
    currency: str          # Expected currency symbol / ISO code
    accept_language: str   # Accept-Language header
    not_found_phrases: tuple = field(default_factory=tuple)
    unavailable_phrases: tuple = field(default_factory=tuple)

    @property
    def host(self) -> str:
        return f"www.amazon.{self.domain}"

    def product_url(self, asin: str) -> str:
        return f"https://{self.host}/dp/{asin}"


# English default phrases (used as fallback in any locale)
_EN_NOT_FOUND = (
    "Page Not Found",
    "Looking for something",
    "We couldn't find that page",
    "We cannot find the page",
    "Sorry! We couldn't find",
)
_EN_UNAVAILABLE = (
    "Currently unavailable",
    "We don't know when or if this item will be back in stock",
    "This title is not currently available for purchase",
)

MARKETPLACES: list[Marketplace] = [
    Marketplace(
        code="IN", country="India", domain="in", currency="INR",
        accept_language="en-IN,en;q=0.9,hi;q=0.8",
        not_found_phrases=_EN_NOT_FOUND,
        unavailable_phrases=_EN_UNAVAILABLE + ("फ़िलहाल उपलब्ध नहीं है",),
    ),
    Marketplace(
        code="US", country="United States", domain="com", currency="USD",
        accept_language="en-US,en;q=0.9",
        not_found_phrases=_EN_NOT_FOUND,
        unavailable_phrases=_EN_UNAVAILABLE,
    ),
    Marketplace(
        code="UK", country="United Kingdom", domain="co.uk", currency="GBP",
        accept_language="en-GB,en;q=0.9",
        not_found_phrases=_EN_NOT_FOUND,
        unavailable_phrases=_EN_UNAVAILABLE,
    ),
    Marketplace(
        code="CA", country="Canada", domain="ca", currency="CAD",
        accept_language="en-CA,en;q=0.9,fr-CA;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + ("Page introuvable", "Désolé, nous n'avons pas trouvé"),
        unavailable_phrases=_EN_UNAVAILABLE + ("Actuellement indisponible",),
    ),
    Marketplace(
        code="AU", country="Australia", domain="com.au", currency="AUD",
        accept_language="en-AU,en;q=0.9",
        not_found_phrases=_EN_NOT_FOUND,
        unavailable_phrases=_EN_UNAVAILABLE,
    ),
    Marketplace(
        code="DE", country="Germany", domain="de", currency="EUR",
        accept_language="de-DE,de;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Die von Ihnen angeforderte Seite wurde nicht gefunden",
            "Seite nicht gefunden",
            "Tut uns leid",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("Derzeit nicht verfügbar",),
    ),
    Marketplace(
        code="FR", country="France", domain="fr", currency="EUR",
        accept_language="fr-FR,fr;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Page introuvable",
            "Désolé, nous n'avons pas trouvé",
            "Nous n'avons pas trouvé cette page",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("Actuellement indisponible",),
    ),
    Marketplace(
        code="IT", country="Italy", domain="it", currency="EUR",
        accept_language="it-IT,it;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Pagina non trovata",
            "Ci dispiace",
            "non siamo riusciti a trovare",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("Attualmente non disponibile",),
    ),
    Marketplace(
        code="ES", country="Spain", domain="es", currency="EUR",
        accept_language="es-ES,es;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Página no encontrada",
            "no hemos podido encontrar la página",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("No disponible por el momento", "Actualmente no disponible"),
    ),
    Marketplace(
        code="JP", country="Japan", domain="co.jp", currency="JPY",
        accept_language="ja-JP,ja;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "お探しのページが見つかりません",
            "ページが見つかりません",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("現在在庫切れです", "現在お取り扱いできません"),
    ),
    Marketplace(
        code="NL", country="Netherlands", domain="nl", currency="EUR",
        accept_language="nl-NL,nl;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Pagina niet gevonden",
            "We kunnen de pagina",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("Momenteel niet beschikbaar",),
    ),
    Marketplace(
        code="BR", country="Brazil", domain="com.br", currency="BRL",
        accept_language="pt-BR,pt;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Página não encontrada",
            "Não foi possível encontrar",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("Indisponível no momento", "Atualmente indisponível"),
    ),
    Marketplace(
        code="MX", country="Mexico", domain="com.mx", currency="MXN",
        accept_language="es-MX,es;q=0.9,en;q=0.8",
        not_found_phrases=_EN_NOT_FOUND + (
            "Página no encontrada",
            "no hemos podido encontrar la página",
        ),
        unavailable_phrases=_EN_UNAVAILABLE + ("No disponible por el momento",),
    ),
]

MARKETPLACES_BY_CODE: dict[str, Marketplace] = {m.code: m for m in MARKETPLACES}
