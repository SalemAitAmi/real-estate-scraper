"""
Detail-page enrichment orchestrator.

Model A: sequential enrichment using the scraper's own browser session.
Future models will add parallelism (B/C) and deferred execution (D).
"""

import logging
from typing import List

from data.models import RentalListing

logger = logging.getLogger(__name__)


class DetailEnricher:
    """Enriches listing stubs with data from their detail pages.

    In Model A the enricher delegates to the scraper's
    ``enrich_listings`` method, which navigates to each detail URL
    in the same browser session that collected the stubs.

    The class exists as a stable interface: the orchestrator always
    goes through ``DetailEnricher.enrich()``, so upgrading to a
    thread-pool (Model B) or deferred-run (Model D) requires changes
    only inside this class.
    """

    def __init__(self, scraper):
        """
        Args:
            scraper: A started ``BaseScraper`` subclass instance with
                     an active browser session.
        """
        self.scraper = scraper

    def enrich(self, stubs: List[RentalListing]) -> List[RentalListing]:
        """Fetch detail pages for *stubs* and return enriched listings.

        The returned list may be longer than *stubs* (e.g. rentals.ca
        expands one stub into multiple floor-plan listings) or shorter
        (if a detail page fails and no fallback is possible).
        """
        if not stubs:
            return stubs

        site = self.scraper.SITE_NAME
        logger.info(
            f"\n{'='*60}\n"
            f"ENRICHING {len(stubs)} STUBS ({site})\n"
            f"{'='*60}"
        )

        enriched = self.scraper.enrich_listings(stubs)

        logger.info(
            f"{site}: {len(stubs)} stubs → {len(enriched)} enriched listings"
        )
        return enriched