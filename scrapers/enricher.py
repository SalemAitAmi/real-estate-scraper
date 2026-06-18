"""
Detail-page enrichment orchestrator.
Saves selector catalog on completion.
"""

import logging
from typing import List
from data.models import RentalListing
from .base_scraper import SelectorCatalog

logger = logging.getLogger(__name__)


class DetailEnricher:
    def __init__(self, scraper):
        self.scraper = scraper

    def enrich(self, stubs: List[RentalListing]) -> List[RentalListing]:
        if not stubs: return stubs
        site = self.scraper.SITE_NAME
        logger.info("\n%s\nENRICHING %d STUBS (%s)\n%s",
                     "=" * 60, len(stubs), site, "=" * 60)
        enriched = self.scraper.enrich_listings(stubs)
        SelectorCatalog.save()
        logger.info("%s: %d stubs → %d enriched", site, len(stubs), len(enriched))
        return enriched