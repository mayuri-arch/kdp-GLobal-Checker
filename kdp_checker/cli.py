"""Command-line interface for the KDP revenue intelligence checker."""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import click
from rich.console import Console

from .checker import MarketplaceChecker
from .email_gen import generate_emails
from .intelligence import analyze_results
from .marketplaces import MARKETPLACES, MARKETPLACES_BY_CODE
from .reporter import export_csv, export_json, render_console


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _validate_asin(ctx, param, value: str) -> str:
    value = (value or "").strip().upper()
    if not _ASIN_RE.match(value):
        raise click.BadParameter("ASIN must be 10 alphanumeric characters")
    return value


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("asin", callback=_validate_asin)
@click.option("--markets", "-m", default=None, help="Comma-separated codes (US,UK,IN...)")
@click.option("--concurrency", "-c", default=4, show_default=True, type=int)
@click.option("--retries", "-r", default=3, show_default=True, type=int)
@click.option("--timeout", default=20.0, show_default=True, type=float)
@click.option("--proxy", default=None, help="HTTP proxy URL")
@click.option("--browser-proxy", default=None, help="Playwright proxy URL")
@click.option("--no-browser", is_flag=True, default=False, help="Disable Playwright fallback")
@click.option("--json-out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--csv-out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--author", default=None, help="Author name (fills KDP email templates)")
@click.option("--title", "book_title", default=None, help="Book title (fills KDP email templates)")
def main(asin, markets, concurrency, retries, timeout, proxy, browser_proxy,
         no_browser, json_out, csv_out, author, book_title):
    """Run a full global availability + revenue intelligence check."""
    if markets:
        codes = [c.strip().upper() for c in markets.split(",") if c.strip()]
        unknown = [c for c in codes if c not in MARKETPLACES_BY_CODE]
        if unknown:
            click.echo(f"Unknown: {', '.join(unknown)}", err=True)
            click.echo(f"Available: {', '.join(m.code for m in MARKETPLACES)}", err=True)
            sys.exit(2)
        targets = [MARKETPLACES_BY_CODE[c] for c in codes]
    else:
        targets = MARKETPLACES

    console = Console()
    console.print(f"[bold]Checking {asin} across {len(targets)} marketplace(s)...[/]")

    checker = MarketplaceChecker(
        concurrency=concurrency, max_retries=retries, timeout_s=timeout,
        proxy=proxy, browser_proxy=browser_proxy,
        use_browser_fallback=not no_browser,
    )

    def progress(r):
        a = r.analysis
        tag = a.status.label
        extra = f"(browser)" if r.used_browser else ""
        console.print(f"  {tag} {r.marketplace.country:<15} amazon.{r.marketplace.domain:<8} "
                      f"[dim]{(a.reason or a.error or '')} {extra}[/]")

    results = asyncio.run(checker.run(asin, targets, progress_cb=progress))
    report = analyze_results(asin, results)
    emails = generate_emails(asin, report, results, author, book_title)

    console.print()
    render_console(results, report, emails, asin, console)

    if json_out:
        export_json(results, json_out, asin, report, emails)
        console.print(f"[dim]JSON → {json_out}[/]")
    if csv_out:
        export_csv(results, csv_out)
        console.print(f"[dim]CSV → {csv_out}[/]")


if __name__ == "__main__":
    main()
