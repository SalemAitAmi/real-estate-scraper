"""
Base scraper: undetected_chromedriver + Selenium.

Provides the shared multi-location orchestration loop (including the
``skip_covered_locations`` toggle) and common browser helpers.

Scraping is split into two phases:

1. **Stub collection** — ``scrape_locations`` paginates through search
   results and returns lightweight listing stubs.
2. **Detail enrichment** — ``enrich_listings`` visits each stub's
   detail page and fills in the remaining fields.  The orchestrator
   (``run_scrapers.py``) decides whether and when to run this phase.
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Any

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
)

from data.models import RentalListing

logger = logging.getLogger(__name__)


@dataclass
class ScraperStats:
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    pages_scraped: int = 0
    listings_found: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "pages_scraped": self.pages_scraped,
            "listings_found": self.listings_found,
            "errors": self.errors[-10:],
        }


class BaseScraper(ABC):
    SITE_NAME: str = "base"
    BASE_URL: str = ""

    SHORT_DELAY = (1.0, 2.0)
    MEDIUM_DELAY = (3.0, 4.0)
    LONG_DELAY = (5.0, 6.0)
    PAGE_LOAD_DELAY = (5.0, 5.5)

    def __init__(
        self,
        headless: bool = False,
        skip_covered_locations: bool = True,
        max_price: Optional[int] = None,
        min_price: Optional[int] = None,
        min_beds: Optional[int] = None,
        max_beds: Optional[int] = None,
        min_baths: Optional[int] = None,
        max_baths: Optional[int] = None,
        min_sqft: Optional[int] = None,
        max_sqft: Optional[int] = None,
    ):
        self.headless = headless
        self.skip_covered_locations = skip_covered_locations
        self.max_price = max_price
        self.min_price = min_price
        self.min_beds = min_beds
        self.max_beds = max_beds
        self.min_baths = min_baths
        self.max_baths = max_baths
        self.min_sqft = min_sqft
        self.max_sqft = max_sqft

        self.driver: Optional[uc.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self.stats = ScraperStats()
        self._stop_pagination = False
        self._seen_ids: Set[str] = set()
        self._seen_cities: Set[str] = set()

    # ── Browser lifecycle ──────────────────────────────────────────

    def _create_driver(self) -> uc.Chrome:
        options = uc.ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-CA")
        return uc.Chrome(options=options, version_main=148)

    def start(self):
        logger.info(f"Starting {self.SITE_NAME} scraper")
        self.driver = self._create_driver()
        self.wait = WebDriverWait(self.driver, 15)
        self.stats = ScraperStats()

    def stop(self):
        self.stats.end_time = datetime.now()
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        logger.info(f"Stopped {self.SITE_NAME} scraper")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ── Delay helpers ──────────────────────────────────────────────

    def delay(self, delay_range: tuple = None):
        time.sleep(random.uniform(*(delay_range or self.MEDIUM_DELAY)))

    def short_delay(self):
        self.delay(self.SHORT_DELAY)

    def medium_delay(self):
        self.delay(self.MEDIUM_DELAY)

    def long_delay(self):
        self.delay(self.LONG_DELAY)

    # ── Navigation / element helpers ───────────────────────────────

    def navigate(self, url: str):
        logger.info(f"Navigating to: {url}")
        self.driver.get(url)
        self.delay(self.PAGE_LOAD_DELAY)

    def wait_for_element(self, by: By, selector: str, timeout: int = 15):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )

    def wait_for_clickable(self, by: By, selector: str, timeout: int = 15):
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, selector))
        )

    def find_element_safe(self, by: By, selector: str):
        try:
            return self.driver.find_element(by, selector)
        except NoSuchElementException:
            return None

    def find_elements_safe(self, by: By, selector: str) -> List:
        try:
            return self.driver.find_elements(by, selector)
        except NoSuchElementException:
            return []

    def scroll_down(self, pixels: int = 500):
        self.driver.execute_script(f"window.scrollBy(0, {pixels});")
        self.short_delay()

    def scroll_to_bottom(self):
        last = self.driver.execute_script("return document.body.scrollHeight")
        while True:
            self.scroll_down(random.randint(400, 800))
            self.short_delay()
            cur = self.driver.execute_script("return document.body.scrollHeight")
            if cur == last:
                break
            last = cur

    def get_page_source(self) -> str:
        return self.driver.page_source

    def type_slowly(self, element, text: str):
        for ch in text:
            element.send_keys(ch)
            time.sleep(random.uniform(0.05, 0.15))

    # ── Popup dismissal (override in subclasses for site specifics) ─

    def _dismiss_popups(self):
        for sel in (
            "button#onetrust-accept-btn-handler",
            "button.accept-cookies",
            "[aria-label='Close']",
            ".modal-close",
            "button.close",
        ):
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    btn.click()
                    self.short_delay()
            except Exception:
                continue

    # ── Multi-location orchestration ───────────────────────────────

    def scrape_locations(
        self,
        locations: List[str],
        max_pages: int = 50,
    ) -> List[RentalListing]:
        """Collect listing stubs from every *location*.

        Returns raw stubs only — detail-page enrichment is handled
        separately by :class:`DetailEnricher`.
        """
        all_listings: List[RentalListing] = []
        self._seen_ids.clear()
        self._seen_cities.clear()

        if not locations:
            return all_listings

        remaining = list(locations)
        first = remaining.pop(0)

        logger.info(f"Searching primary location: {first}")
        all_listings.extend(self.scrape_city(first, max_pages=max_pages))
        logger.info(f"Cities found so far: {sorted(self._seen_cities)}")

        # Decide what still needs searching
        to_search: List[str] = []
        for loc in remaining:
            if self.skip_covered_locations and self._is_location_covered(loc):
                logger.info(f"'{loc}' already covered — skipping")
            else:
                to_search.append(loc)

        for loc in to_search:
            logger.info(f"\n{'='*60}\nSearching additional location: {loc}\n{'='*60}")
            self._stop_pagination = False
            self.long_delay()
            listings = self.scrape_city(loc, max_pages=max_pages)
            all_listings.extend(listings)
            logger.info(f"Found {len(listings)} listings in {loc}")

        logger.info(f"Total unique stubs: {len(all_listings)}")
        return all_listings

    def _is_location_covered(self, location: str) -> bool:
        loc = location.lower().strip()
        return any(
            loc in s.lower() or s.lower() in loc for s in self._seen_cities
        )

    # ── Detail enrichment (subclass hook) ──────────────────────────

    def enrich_listings(
        self, stubs: List[RentalListing]
    ) -> List[RentalListing]:
        """Visit detail pages and return enriched listings.

        Override in subclasses.  The default implementation returns
        *stubs* unchanged (no detail pages to fetch).
        """
        return stubs

    # ── Single-city pagination loop ────────────────────────────────

    def scrape_city(self, city_name: str, max_pages: int = 50) -> List[RentalListing]:
        all_listings: List[RentalListing] = []
        self._stop_pagination = False

        logger.info(f"Searching for rentals in: {city_name}")
        if not self.search_city(city_name):
            logger.error(f"Failed to search for {city_name}")
            return all_listings

        page = 1
        while page <= max_pages:
            logger.info(f"Scraping page {page} for {city_name}")
            try:
                listings = self.get_listings_from_page()
                all_listings.extend(listings)
                self.stats.pages_scraped += 1
                self.stats.listings_found += len(listings)
                logger.info(f"Found {len(listings)} listings on page {page}")

                if self._stop_pagination:
                    logger.info("Stop condition reached")
                    break
                if not listings:
                    logger.info("No listings found, stopping")
                    break

                self.long_delay()
                if not self.go_to_next_page():
                    logger.info("No more pages")
                    break
                page += 1

            except Exception as exc:
                logger.error(f"Error on page {page}: {exc}")
                self.stats.errors.append(str(exc))
                break

        return all_listings

    # ── Abstract interface ─────────────────────────────────────────

    @abstractmethod
    def search_city(self, city_name: str) -> bool:
        ...

    @abstractmethod
    def get_listings_from_page(self) -> List[RentalListing]:
        ...

    @abstractmethod
    def go_to_next_page(self) -> bool:
        ...

    # ── Debug helper ───────────────────────────────────────────────

    def _save_debug_html(self, html: str, tag: str):
        from pathlib import Path
        try:
            d = Path("./data/debug_html")
            d.mkdir(parents=True, exist_ok=True)
            ts = int(datetime.now().timestamp())
            (d / f"{self.SITE_NAME.replace('.','_')}_{tag}_{ts}.html").write_text(
                html, encoding="utf-8"
            )
        except Exception:
            pass