"""
Scraper orchestrator.

Two usage modes:
  • Excel UDF:  =scrape_now()    (async, non-blocking)
                =scrape_status() (cheap polling cell)
  • CLI:        py run_scrapers.py
"""

import logging
import threading
from datetime import datetime
from typing import List, Optional

import xlwings as xw

from config.settings import get_settings
from data.models import RentalListing
from data.normalizer import deduplicate_listings, normalize_listing
from data.store import ListingStore
from scrapers import (
    ApartmentsComScraper,
    DetailEnricher,
    RealtorCaScraper,
    RentalsCaScraper,
)

logger = logging.getLogger(__name__)

SCRAPERS = {
    "realtor.ca":     RealtorCaScraper,
    "rentals.ca":     RentalsCaScraper,
    "apartments.com": ApartmentsComScraper,
}

# ── Shared run-state, observed by scrape_status() ──────────────────
_run_lock = threading.Lock()
_last_summary: str = "Idle"


def _set_status(msg: str):
    global _last_summary
    _last_summary = msg


# ── Synchronous core (used by both UDF and CLI) ────────────────────

def run_all_scrapers(settings) -> List[RentalListing]:
    all_listings: List[RentalListing] = []
    for site in settings.enabled_sites:
        cls = SCRAPERS.get(site)
        if cls is None:
            logger.warning(f"No scraper registered for site: {site}")
            continue
        logger.info(f"\n=== {site} ===")
        try:
            with cls(
                headless=settings.search.headless,
                skip_covered_locations=settings.search.skip_covered_locations,
                min_price=settings.search.min_price,
                max_price=settings.search.max_price,
                min_beds=settings.search.min_bedrooms,
                max_beds=settings.search.max_bedrooms,
                min_baths=settings.search.min_bathrooms,
                max_baths=settings.search.max_bathrooms,
                min_sqft=settings.search.min_sqft,
                max_sqft=settings.search.max_sqft,
            ) as scraper:
                stubs = scraper.scrape_locations(
                    settings.search.locations,
                    max_pages=settings.search.max_pages,
                )
                if settings.search.fetch_details:
                    listings = DetailEnricher(scraper).enrich(stubs)
                else:
                    listings = stubs
                all_listings.extend(listings)
        except Exception as exc:
            logger.error(f"{site} run failed: {exc}", exc_info=True)
    return all_listings


def _ingest(listings: List[RentalListing]) -> str:
    """Normalize → dedupe → merge into store. Returns a summary string."""
    listings = [normalize_listing(l) for l in listings]
    listings = deduplicate_listings(listings)
    store = ListingStore()
    report = store.merge_results(listings)
    store.save()
    return report.summary()


# ── UDFs ───────────────────────────────────────────────────────────

@xw.func(async_mode="threading")
def scrape_now(caller) -> str:
    """``=scrape_now()`` — run all enabled scrapers asynchronously.

    While the function is computing, Excel displays ``#GETTING_DATA``
    in the host cell, which is the natural 'in progress' indicator —
    no separate disable-button bookkeeping required.  Re-entry is
    blocked by ``_run_lock``: a second call returns immediately with
    a 'busy' marker.
    """
    if not _run_lock.acquire(blocking=False):
        return f"{_last_summary} (already running)"
    try:
        _set_status("Running…")
        started = datetime.now()
        # Pull the latest config from settings.json (the Config-sheet
        # sync happens on the Excel side via write-through cells; see
        # ExcelInterface._sync_config_in()).
        settings = get_settings()
        listings = run_all_scrapers(settings)
        summary = _ingest(listings)
        elapsed = (datetime.now() - started).total_seconds()
        msg = (
            f"Done {datetime.now():%H:%M:%S} "
            f"({elapsed:.0f}s) — {summary}"
        )
        _set_status(msg)
        return msg
    except Exception as exc:
        msg = f"Error: {exc}"
        _set_status(msg)
        logger.exception("scrape_now failed")
        return msg
    finally:
        _run_lock.release()


@xw.func
def scrape_status(caller) -> str:
    """``=scrape_status()`` — cheap snapshot of the last run's result."""
    return _last_summary


# ── CLI entry point ────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    listings = run_all_scrapers(settings)
    print(_ingest(listings))


if __name__ == "__main__":
    main()