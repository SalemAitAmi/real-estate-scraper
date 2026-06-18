"""
Base scraper with selector-catalog infrastructure, shared parsers,
and common browser helpers.  version_main pinned to 148.
"""

import json, logging, random, re, time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    ElementClickInterceptedException,
)

from data.models import (
    PropertyType, HeatingType, ParkingType, LaundryType,
    RentalListing, RentValue,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
#  Selector catalog
# ────────────────────────────────────────────────────────────────────

class SelectorCatalog:
    _PATH = Path(__file__).resolve().parent.parent / "data" / "selector_misses.json"
    _misses: List[Dict[str, str]] = []

    @classmethod
    def record(cls, site, name, selector, url=""):
        cls._misses.append({
            "site": site, "name": name, "selector": selector[:200],
            "url": url[:200], "ts": datetime.now().isoformat()})
        logger.warning("SELECTOR_MISS [%s] %s → %s", site, name, selector[:80])

    @classmethod
    def save(cls):
        if not cls._misses: return
        existing = []
        if cls._PATH.exists():
            try: existing = json.loads(cls._PATH.read_text("utf-8"))
            except Exception: pass
        existing.extend(cls._misses)
        cls._PATH.parent.mkdir(parents=True, exist_ok=True)
        cls._PATH.write_text(json.dumps(existing[-500:], indent=2), "utf-8")
        cls._misses.clear()

    @classmethod
    def recent(cls, site=None):
        if not cls._PATH.exists(): return []
        data = json.loads(cls._PATH.read_text("utf-8"))
        return [m for m in data if m["site"] == site] if site else data

# ────────────────────────────────────────────────────────────────────
#  Shared parsers
# ────────────────────────────────────────────────────────────────────

_PROP_TYPE_MAP = {
    "apartment": PropertyType.APARTMENT, "condo": PropertyType.CONDO,
    "condominium": PropertyType.CONDO, "house": PropertyType.HOUSE,
    "townhouse": PropertyType.TOWNHOUSE, "townhome": PropertyType.TOWNHOUSE,
    "row": PropertyType.TOWNHOUSE, "duplex": PropertyType.DUPLEX,
    "triplex": PropertyType.TRIPLEX, "studio": PropertyType.STUDIO,
    "bachelor": PropertyType.STUDIO, "loft": PropertyType.LOFT,
    "basement": PropertyType.BASEMENT, "room": PropertyType.ROOM,
}
_HEAT_MAP = {
    "electric": HeatingType.ELECTRIC, "gas": HeatingType.GAS,
    "oil": HeatingType.OIL, "hydronic": HeatingType.HYDRONIC,
    "hot water": HeatingType.HYDRONIC, "radiant": HeatingType.RADIANT,
    "forced air": HeatingType.FORCED_AIR, "heat pump": HeatingType.HEAT_PUMP,
    "baseboard": HeatingType.BASEBOARD, "central": HeatingType.CENTRAL,
}
_PARKING_MAP = {
    "underground": ParkingType.UNDERGROUND, "indoor": ParkingType.INDOOR,
    "garage": ParkingType.INDOOR, "attached": ParkingType.INDOOR,
    "outdoor": ParkingType.OUTDOOR, "surface": ParkingType.OUTDOOR,
    "street": ParkingType.STREET, "none": ParkingType.NONE,
}
_AMENITY_FLAGS = {
    "dishwasher": ("amenities", "dishwasher"),
    "gym": ("amenities", "gym"), "fitness": ("amenities", "gym"),
    "pool": ("amenities", "pool"), "elevator": ("amenities", "elevator"),
    "concierge": ("amenities", "concierge"),
    "doorman": ("amenities", "concierge"),
    "rooftop": ("amenities", "rooftop"),
    "balcony": ("features", "balcony"), "patio": ("features", "balcony"),
    "a/c": ("features", "air_conditioning"),
    "air condition": ("features", "air_conditioning"),
}

def parse_price(text):
    if not text: return None
    vals = [float(n.replace(",", ""))
            for n in re.findall(r"\$?\s*([\d,]+)", text.replace(" ", ""))]
    vals = [v for v in vals if 300 <= v <= 50000]
    return min(vals) if vals else None

def parse_rent(text):
    if not text: return None
    t = text.replace(",", "").replace("\u2013", "-").replace("\u2014", "-")
    nums = [float(n) for n in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t)]
    nums = [n for n in nums if 100 <= n <= 50000]
    if not nums: return None
    if len(nums) >= 2 and max(nums) != min(nums):
        lo, hi = min(nums), max(nums)
        return RentValue(amount=lo, min_amount=lo, max_amount=hi)
    return RentValue(amount=nums[0])

def parse_beds(text):
    if not text: return None
    t = text.lower().strip()
    if "studio" in t or "bachelor" in t: return 0
    if "+" in t:
        parts = [p.strip() for p in t.split("+") if p.strip().isdigit()]
        if parts: return sum(int(p) for p in parts)
    m = re.search(r"(\d+)\s*(?:bd|bed|br|bdrm|bedroom)?\b", t)
    return int(m.group(1)) if m else None

def parse_baths(text):
    if not text: return None
    s = text.replace("\u00bd", ".5").replace("1/2", ".5").lower()
    m = re.search(r"([\d.]+)\s*(?:ba|bath)?", s)
    return float(m.group(1)) if m else None

def parse_sqft(text):
    if not text: return None
    nums = [int(n.replace(",", "")) for n in re.findall(r"(\d[\d,]*)", text)]
    nums = [n for n in nums if n > 50]
    return min(nums) if nums else None

def parse_int_val(text):
    if not text: return None
    m = re.search(r"(\d+)", text.replace(",", ""))
    return int(m.group(1)) if m else None

def parse_property_type(text):
    if not text: return PropertyType.APARTMENT
    s = text.lower()
    for k, v in _PROP_TYPE_MAP.items():
        if k in s: return v
    return PropertyType.OTHER

def parse_heating_type(text):
    if not text: return HeatingType.UNKNOWN
    s = text.lower()
    for k, v in _HEAT_MAP.items():
        if k in s: return v
    return HeatingType.UNKNOWN

def parse_parking_type(text):
    if not text: return None
    s = text.lower()
    for k, v in _PARKING_MAP.items():
        if k in s: return v
    return ParkingType.OUTDOOR

def apply_amenity_flag(listing, text):
    lo = text.lower()
    for kw, (obj_attr, field_name) in _AMENITY_FLAGS.items():
        if kw in lo and ("carpool" not in lo or kw != "pool"):
            setattr(getattr(listing, obj_attr), field_name, True)
    if "washer" in lo or "laundry" in lo:
        if "in-unit" in lo or "in unit" in lo or "suite" in lo:
            listing.features.laundry = LaundryType.IN_UNIT
        elif "hookup" in lo:
            listing.features.laundry = LaundryType.HOOKUPS
        elif "shared" in lo or "common" in lo or "building" in lo:
            listing.features.laundry = LaundryType.IN_BUILDING
    if "parking" in lo or "garage" in lo:
        listing.features.parking_type = ParkingType.INDOOR

def parse_time_on_site(text):
    if not text: return None
    lo = text.lower().strip(); now = datetime.now()
    try:
        if "day" in lo:
            d = parse_int_val(lo) or (0 if "<" in lo else 1)
            return datetime(now.year, now.month, now.day) - timedelta(days=d)
        if "week" in lo:
            return datetime(now.year, now.month, now.day) - timedelta(
                weeks=parse_int_val(lo) or 1)
        if "month" in lo:
            return datetime(now.year, now.month, now.day) - timedelta(
                days=(parse_int_val(lo) or 1) * 30)
    except Exception: pass
    return None


@dataclass
class ScraperStats:
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    pages_scraped: int = 0
    listings_found: int = 0
    errors: List[str] = field(default_factory=list)
    def to_dict(self):
        return {"start_time": self.start_time.isoformat(),
                "end_time": self.end_time.isoformat() if self.end_time else None,
                "pages_scraped": self.pages_scraped,
                "listings_found": self.listings_found,
                "errors": self.errors[-10:]}


# ════════════════════════════════════════════════════════════════════

class BaseScraper(ABC):
    SITE_NAME: str = "base"
    BASE_URL: str = ""
    SELECTORS: Dict[str, Any] = {}

    SHORT_DELAY = (1.0, 2.0)
    MEDIUM_DELAY = (3.0, 4.0)
    LONG_DELAY = (5.0, 6.0)
    PAGE_LOAD_DELAY = (5.0, 5.5)

    # Uniform button‐like constants live here for shared reference
    _BTN_W, _BTN_H, _BTN_GAP, _BTN_TOP = 90, 28, 8, 7

    def __init__(self, headless=False, skip_covered_locations=True,
                 max_price=None, min_price=None,
                 min_beds=None, max_beds=None,
                 min_baths=None, max_baths=None,
                 min_sqft=None, max_sqft=None):
        self.headless = headless
        self.skip_covered_locations = skip_covered_locations
        self.max_price = max_price; self.min_price = min_price
        self.min_beds = min_beds;   self.max_beds = max_beds
        self.min_baths = min_baths; self.max_baths = max_baths
        self.min_sqft = min_sqft;   self.max_sqft = max_sqft
        self.driver: Optional[uc.Chrome] = None
        self.stats = ScraperStats()
        self._stop_pagination = False
        self._seen_ids: Set[str] = set()
        self._seen_cities: Set[str] = set()

    # ── Browser lifecycle ──────────────────────────────────────────

    def start(self):
        logger.info("Starting %s scraper", self.SITE_NAME)
        opts = uc.ChromeOptions()
        if self.headless: opts.add_argument("--headless=new")
        for a in ("--no-sandbox", "--disable-dev-shm-usage",
                   "--disable-blink-features=AutomationControlled",
                   "--window-size=1920,1080", "--lang=en-CA"):
            opts.add_argument(a)
        self.driver = uc.Chrome(options=opts, version_main=148)
        self.stats = ScraperStats()

    def stop(self):
        self.stats.end_time = datetime.now()
        SelectorCatalog.save()
        if self.driver:
            try: self.driver.quit()
            except Exception: pass
            try: self.driver.quit = lambda *a, **kw: None
            except Exception: pass
            self.driver = None
        logger.info("Stopped %s scraper", self.SITE_NAME)

    def __enter__(self):  self.start(); return self
    def __exit__(self, *_): self.stop()

    # ── Selector resolution ────────────────────────────────────────

    def _sel(self, name, **fmt):
        node = self.SELECTORS
        for part in name.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise KeyError(f"Selector '{name}' not in {self.SITE_NAME}")
        s = str(node)
        return s.format(**fmt) if fmt else s

    def _miss(self, name, sel):
        url = ""
        try: url = self.driver.current_url
        except Exception: pass
        SelectorCatalog.record(self.SITE_NAME, name, sel, url)

    # ── Selenium helpers ───────────────────────────────────────────

    def css(self, name, *, timeout=0, **fmt):
        sel = self._sel(name, **fmt)
        try:
            if timeout:
                return WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            return self.driver.find_element(By.CSS_SELECTOR, sel)
        except (NoSuchElementException, TimeoutException):
            self._miss(name, sel); return None

    def css_all(self, name, **fmt):
        sel = self._sel(name, **fmt)
        try: return self.driver.find_elements(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            self._miss(name, sel); return []

    def css_click(self, name, *, timeout=0, **fmt):
        el = self.css(name, timeout=timeout, **fmt)
        if not el: return False
        self.safe_click(el); return True

    # ── BeautifulSoup helpers ──────────────────────────────────────

    def soup_el(self, parent, name, **fmt):
        sel = self._sel(name, **fmt)
        el = parent.select_one(sel)
        if el is None: self._miss(name, sel)
        return el

    def soup_text(self, parent, name, **fmt):
        el = self.soup_el(parent, name, **fmt)
        return el.get_text(" ", strip=True) if el else ""

    def soup_all(self, parent, name, **fmt):
        return parent.select(self._sel(name, **fmt))

    # ── Common browser actions ─────────────────────────────────────

    def safe_click(self, el):
        try: el.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", el)

    def navigate(self, url):
        logger.info("Navigating to: %s", url)
        self.driver.get(url); self.delay(self.PAGE_LOAD_DELAY)

    def delay(self, rng=None):
        time.sleep(random.uniform(*(rng or self.MEDIUM_DELAY)))
    def short_delay(self):  self.delay(self.SHORT_DELAY)
    def medium_delay(self): self.delay(self.MEDIUM_DELAY)
    def long_delay(self):   self.delay(self.LONG_DELAY)

    def get_page_source(self): return self.driver.page_source

    def type_slowly(self, el, text):
        for ch in text:
            el.send_keys(ch); time.sleep(random.uniform(0.05, 0.15))

    def fill_input(self, name, value, *, slow=False, **fmt):
        """Clear and fill a text input. Returns True on success."""
        el = self.css(name, timeout=5, **fmt)
        if not el: return False
        el.click(); time.sleep(0.2); el.clear(); time.sleep(0.1)
        if slow:
            self.type_slowly(el, value)
        else:
            el.send_keys(value)
        time.sleep(0.3)
        return True

    def scroll_page(self, steps=15, lo=300, hi=600):
        try:
            last = 0
            for _ in range(steps):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(lo, hi)});")
                time.sleep(random.uniform(0.2, 0.5))
                cur = self.driver.execute_script("return window.pageYOffset")
                if cur == last: break
                last = cur
            self.driver.execute_script("window.scrollTo(0,0);")
            time.sleep(0.3)
        except Exception: pass

    def scroll_to_element(self, el):
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.4)

    def dismiss_popups(self):
        for sel in ("button#onetrust-accept-btn-handler",
                     "button.accept-cookies", "[aria-label='Close']",
                     ".modal-close", "button.close"):
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed(): btn.click(); self.short_delay()
            except Exception: continue

    # ── On-site inquiry (override in subclasses) ───────────────────

    def send_inquiry(self, listing, *, first_name, last_name,
                     email, auto_send=False, **kwargs) -> bool:
        """Send an inquiry via the site's built-in contact form.

        Returns ``True`` if the form was submitted (or a manual draft
        was prepared).  Subclasses override for site-specific handling.
        """
        raise NotImplementedError(
            f"{self.SITE_NAME} does not implement on-site inquiries")

    # ── Multi-location orchestration ───────────────────────────────

    def scrape_locations(self, locations, max_pages=50):
        all_listings: List[RentalListing] = []
        self._seen_ids.clear(); self._seen_cities.clear()
        if not locations: return all_listings
        remaining = list(locations); first = remaining.pop(0)
        logger.info("Searching primary location: %s", first)
        all_listings.extend(self.scrape_city(first, max_pages=max_pages))
        logger.info("Cities found so far: %s", sorted(self._seen_cities))
        for loc in remaining:
            if self.skip_covered_locations and self._is_covered(loc):
                logger.info("'%s' already covered — skipping", loc); continue
            logger.info("\n%s\nSearching: %s\n%s", "=" * 60, loc, "=" * 60)
            self._stop_pagination = False; self.long_delay()
            listings = self.scrape_city(loc, max_pages=max_pages)
            all_listings.extend(listings)
        logger.info("Total unique stubs: %d", len(all_listings))
        return all_listings

    def _is_covered(self, location):
        loc = location.lower().strip()
        return any(loc in s.lower() or s.lower() in loc
                   for s in self._seen_cities)

    def enrich_listings(self, stubs):
        return stubs

    def scrape_city(self, city_name, max_pages=50):
        all_listings = []; self._stop_pagination = False
        logger.info("Searching for rentals in: %s", city_name)
        if not self.search_city(city_name):
            logger.error("Failed to search for %s", city_name)
            return all_listings
        page = 1
        while page <= max_pages:
            logger.info("Scraping page %d for %s", page, city_name)
            try:
                listings = self.get_listings_from_page()
                all_listings.extend(listings)
                self.stats.pages_scraped += 1
                self.stats.listings_found += len(listings)
                if self._stop_pagination or not listings: break
                self.long_delay()
                if not self.go_to_next_page(): break
                page += 1
            except Exception as exc:
                logger.error("Error on page %d: %s", page, exc)
                self.stats.errors.append(str(exc)); break
        return all_listings

    @abstractmethod
    def search_city(self, city_name): ...
    @abstractmethod
    def get_listings_from_page(self): ...
    @abstractmethod
    def go_to_next_page(self): ...