"""
Scraper orchestrator.

Two usage modes
───────────────
• **Button** — the Excel toolbar's *Scrape* button calls
  ``excel.interface._start_scrape()``, which spawns a background
  thread that calls ``run_all_scrapers`` → ``ingest``.
• **CLI** — ``python run_scrapers.py``
"""

import logging
from typing import List

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


# ── Core pipeline (used by both the Excel button and the CLI) ──────

def run_all_scrapers(settings) -> List[RentalListing]:
    """Run every enabled scraper and return raw listings."""
    all_listings: List[RentalListing] = []
    for site in settings.enabled_sites:
        cls = SCRAPERS.get(site)
        if cls is None:
            logger.warning("No scraper registered for site: %s", site)
            continue
        logger.info("\n=== %s ===", site)
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
            logger.error("%s run failed: %s", site, exc, exc_info=True)
    return all_listings


def ingest(listings: List[RentalListing]) -> str:
    """Normalize → deduplicate → merge into the JSON store.

    Returns a human-readable summary string.
    """
    listings = [normalize_listing(l) for l in listings]
    listings = deduplicate_listings(listings)
    store = ListingStore()
    report = store.merge_results(listings)
    store.save()
    return report.summary()


# ── CLI ────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    listings = run_all_scrapers(settings)
    print(ingest(listings))


if __name__ == "__main__":
    main()