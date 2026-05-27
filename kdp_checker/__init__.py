"""KDP Global Marketplace Availability + Revenue Intelligence."""
from .checker import MarketplaceChecker, CheckResult, check_asin
from .marketplaces import MARKETPLACES, Marketplace
from .detector import AvailabilityStatus, PageAnalysis, analyze_page
from .intelligence import IntelligenceReport, Issue, Severity, FixAction, analyze_results
from .email_gen import EmailDraft, generate_emails
from . import storage

__version__ = "2.0.0"
__all__ = [
    "MarketplaceChecker", "CheckResult", "check_asin",
    "MARKETPLACES", "Marketplace",
    "AvailabilityStatus", "PageAnalysis", "analyze_page",
    "IntelligenceReport", "Issue", "Severity", "FixAction", "analyze_results",
    "EmailDraft", "generate_emails",
    "storage",
]
