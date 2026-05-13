"""
Orchestrator — runs enabled scrapers in two phases:

1. **Stub collection** — paginate through search results.
2. **Detail enrichment** — visit each stub's detail page (optional).

Then normalises, deduplicates, merges into the persistent store,
and prints a summary.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from config.settings import get_settings, SearchParameters
from data.models import RentalListing
from data.normalizer import normalize_listing, deduplicate_listings
from data.store import ListingStore
from scrapers import (
    RealtorCaScraper,
    RentalsCaScraper,
    ApartmentsComScraper,
    DetailEnricher,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Scraper registry ──────────────────────────────────────────────

SCRAPER_MAP = {
    #"realtor.ca":     RealtorCaScraper,
    "rentals.ca":     RentalsCaScraper,
    #"apartments.com": ApartmentsComScraper,
}


# ── Run a single scraper ─────────────────────────────────────────

def run_scraper(
    site: str,
    params: SearchParameters,
) -> List[RentalListing]:
    cls = SCRAPER_MAP.get(site)
    if cls is None:
        logger.warning(f"No scraper registered for '{site}'")
        return []

    scraper = cls(
        headless=params.headless,
        skip_covered_locations=params.skip_covered_locations,
        max_price=params.max_price,
        min_price=params.min_price,
        min_beds=params.min_bedrooms,
        max_beds=params.max_bedrooms,
        min_baths=params.min_bathrooms,
        max_baths=params.max_bathrooms,
        min_sqft=params.min_sqft,
        max_sqft=params.max_sqft,
    )

    all_listings: List[RentalListing] = []

    with scraper:
        logger.info(f"\n{'='*60}")
        logger.info(f"{site.upper()} SCRAPER")
        logger.info(f"{'='*60}")
        logger.info(f"  Locations : {params.locations}")
        logger.info(
            f"  Price     : "
            f"${params.min_price or 'any'} – ${params.max_price or 'any'}"
        )
        logger.info(f"  Beds      : {params.min_bedrooms} – {params.max_bedrooms or '+'}")
        logger.info(f"  Baths     : {params.min_bathrooms} – {params.max_bathrooms or '+'}")
        logger.info(f"  Sq.Ft.    : {params.min_sqft or 'any'} – {params.max_sqft or 'any'}")
        logger.info(f"  Details   : {params.fetch_details}")
        logger.info(f"  Skip cov. : {params.skip_covered_locations}")

        # ── Phase 1: stub collection ─────────────────────────────
        try:
            stubs = scraper.scrape_locations(
                params.locations, max_pages=params.max_pages
            )
        except Exception as exc:
            logger.error(f"Scraper error ({site}): {exc}")
            scraper.stats.errors.append(str(exc))
            stubs = []

        logger.info(
            f"\n{site} — {len(stubs)} stubs, "
            f"{scraper.stats.pages_scraped} pages, "
            f"cities: {sorted(scraper._seen_cities)}"
        )

        # ── Phase 2: detail enrichment ───────────────────────────
        if params.fetch_details and stubs:
            enricher = DetailEnricher(scraper)
            all_listings = enricher.enrich(stubs)
        else:
            all_listings = stubs

    return all_listings


# ── Post-scrape pipeline ─────────────────────────────────────────

def pipeline(
    raw_by_domain: Dict[str, List[RentalListing]],
    store: ListingStore,
) -> ListingStore:
    """Normalise → deduplicate → merge into store."""
    for domain, listings in raw_by_domain.items():
        logger.info(f"\nPipeline: {domain} ({len(listings)} raw)")
        listings = [normalize_listing(l) for l in listings]
        listings = deduplicate_listings(listings)
        logger.info(f"  After dedup: {len(listings)}")
        report = store.merge_results(listings)
        logger.info(f"  Merge: {report.summary()}")
        if report.field_changes:
            for ch in report.field_changes[:20]:
                logger.debug(f"    changed: {ch}")

    store.save()
    return store


# ── Snapshot helper ──────────────────────────────────────────────

def save_snapshot(store: ListingStore, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    data = [l.to_dict() for l in store.listings.values()]

    for path in (
        output_dir / f"listings_{ts}.json",
        output_dir / "latest_listings.json",
    ):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(data)} listings → {path}")


def print_summary(store: ListingStore):
    active = store.get_active()
    by_city: Dict[str, List[RentalListing]] = {}
    for l in active:
        by_city.setdefault(l.address.city or "Unknown", []).append(l)

    print(f"\n{'='*60}")
    print("LISTINGS SUMMARY")
    print(f"{'='*60}")
    print(f"Total in store : {len(store.listings)}")
    print(f"Active         : {len(active)}")
    print(
        f"Selected       : "
        f"{sum(1 for l in store.listings.values() if l.is_selected)}"
    )
    print(
        f"Discarded      : "
        f"{sum(1 for l in store.listings.values() if l.is_discarded)}"
    )

    for city, items in sorted(by_city.items()):
        prices = [x.price.base_rent for x in items if x.price.base_rent > 0]
        print(f"\n  {city}: {len(items)} listings")
        if prices:
            print(
                f"    ${min(prices):,.0f} – ${max(prices):,.0f}  "
                f"(avg ${sum(prices)/len(prices):,.0f})"
            )

    by_beds: Dict[str, int] = {}
    for l in active:
        b = l.features.bedrooms
        label = "Studio" if b == 0 else f"{b} bed" if b is not None else "?"
        by_beds[label] = by_beds.get(label, 0) + 1
    print(f"\n  By bedrooms: {dict(sorted(by_beds.items()))}")


# ── Main ─────────────────────────────────────────────────────────

def main():
    settings = get_settings()
    params = settings.search
    store = ListingStore()

    raw_by_domain: Dict[str, List[RentalListing]] = {}
    for site in settings.enabled_sites:
        raw_by_domain[site] = run_scraper(site, params)

    pipeline(raw_by_domain, store)
    save_snapshot(store, Path("./data"))
    print_summary(store)

    logger.info("Done!")


if __name__ == "__main__":
    main()