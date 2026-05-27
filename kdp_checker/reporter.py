"""Reporting: Rich console table + CSV/JSON export with intelligence report."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from .checker import CheckResult
from .detector import AvailabilityStatus
from .intelligence import IntelligenceReport, Issue, Severity
from .email_gen import EmailDraft


_STATUS_STYLE = {
    AvailabilityStatus.LIVE_OPTIMIZED: "bold green",
    AvailabilityStatus.LIVE_LOW_CONVERSION: "yellow",
    AvailabilityStatus.VISIBLE_NOT_PURCHASABLE: "bold red",
    AvailabilityStatus.NOT_FOUND: "red",
    AvailabilityStatus.BLOCKED: "magenta",
    AvailabilityStatus.RESTRICTED: "cyan",
    AvailabilityStatus.ERROR: "bold red",
}
_SEV_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "bold yellow",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}


def render_console(
    results: Iterable[CheckResult],
    report: IntelligenceReport | None = None,
    emails: list[EmailDraft] | None = None,
    asin: str = "",
    console: Console | None = None,
) -> None:
    console = console or Console()
    results = list(results)

    table = Table(title=f"KDP Global Intelligence — ASIN {asin}",
                  box=box.SIMPLE_HEAD, show_lines=False)
    table.add_column("Country", style="bold")
    table.add_column("Domain")
    table.add_column("Status")
    table.add_column("Buy", justify="center")
    table.add_column("Price")
    table.add_column("Reviews", justify="right")
    table.add_column("Title", max_width=34, overflow="ellipsis")
    table.add_column("ms", justify="right")
    table.add_column("Note", max_width=30, overflow="ellipsis")

    for r in results:
        a = r.analysis
        style = _STATUS_STYLE.get(a.status, "")
        buy = "YES" if a.has_buy_button else ("k" if a.has_kindle_buy else ("c" if a.has_add_to_cart else "-"))
        table.add_row(
            r.marketplace.country,
            f"amazon.{r.marketplace.domain}",
            f"[{style}]{a.status.value}[/]",
            buy,
            a.price_text or "-",
            str(a.review_count) if a.review_count is not None else "-",
            a.title or "-",
            str(r.elapsed_ms),
            a.reason or (a.error or ""),
        )
    console.print(table)

    if report:
        console.print()
        console.print(Panel.fit(
            f"[bold]Revenue score[/] {report.revenue_score}/100   "
            f"[bold]Live[/] {report.live_count}/{report.total}\n"
            f"{report.summary}",
            title=f"Summary · {asin}",
        ))
        if report.issues:
            it = Table(title="Prioritized issues", box=box.SIMPLE)
            it.add_column("Sev", style="bold")
            it.add_column("Market")
            it.add_column("Impact", justify="right")
            it.add_column("Issue")
            it.add_column("Action", style="dim")
            for i in report.issues:
                st = _SEV_STYLE.get(i.severity, "")
                it.add_row(
                    f"[{st}]{i.severity.value.upper()}[/]",
                    i.marketplace_code or "-",
                    str(i.estimated_revenue_impact),
                    i.title,
                    i.fix_action.value,
                )
            console.print(it)
            for i in report.issues[:5]:
                console.print(f"\n[bold]· {i.title}[/]")
                console.print(f"  {i.recommendation}")

    if emails:
        console.print()
        console.print(f"[bold]{len(emails)} KDP support email draft(s) ready.[/]")
        for d in emails:
            console.print(f"  - {d.subject}")


def export_json(
    results: Iterable[CheckResult],
    path: str | Path,
    asin: str,
    report: IntelligenceReport | None = None,
    emails: list[EmailDraft] | None = None,
) -> None:
    payload = {
        "asin": asin,
        "results": [r.to_dict() for r in results],
    }
    if report is not None:
        payload["intelligence"] = report.to_dict()
    if emails:
        payload["emails"] = [e.to_dict() for e in emails]
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def export_csv(results: Iterable[CheckResult], path: str | Path) -> None:
    rows = [r.to_dict() for r in results]
    p = Path(path)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    # Flatten signals list
    for r in rows:
        r["signals"] = "|".join(r.get("signals") or [])
    fieldnames = list(rows[0].keys())
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
