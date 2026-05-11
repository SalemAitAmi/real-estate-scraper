"""
Rentals.ca scraper — Selenium + undetected_chromedriver.
"""

import copy
import logging
import random
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup, Tag
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
)

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, ParkingType,
    LaundryType, RentalListing,
)
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
#  Selectors — every selector is a complete, ready-to-use string.
#  No templates, no .format() calls.
# ────────────────────────────────────────────────────────────────────

_PANEL = (
    ".page-search-results__overlay-panels "
    ".page-panel__content-container > div"
)

_BEDS = f"{_PANEL} > div:nth-child(5) > div > div > div"
_BATHS = f"{_PANEL} > div:nth-child(6) > div > div > div"

SEL = {
    # ── Home page ────────────────────────────────────────────────
    "search_input":  ".page-home__hero-search .search-input input",
    "search_button": ".page-home__hero-search .search-input button",

    # ── Results page — view toggle ───────────────────────────────
    "list_view_btn": ".place-view-selector a.first",

    # ── "All Filters" button ─────────────────────────────────────
    "filters_button": "div.filters-bar.d-none.d-lg-flex > button",

    # ── Filter panel — close button ──────────────────────────────
    "panel_close": ".page-panel__title-container button",

    # ── Filter panel — price inputs ──────────────────────────────
    "price_min": f"{_PANEL} > div:nth-child(4) > div > div > div > input:nth-child(1)",
    "price_max": f"{_PANEL} > div:nth-child(4) > div > div > div > input:nth-child(3)",

    # ── Filter panel — individual bedroom options ────────────────
    #    0 = Studio, 1, 2, 3, 4+
    "beds_0": f"{_BEDS} > div:nth-child(1) > label",
    "beds_1": f"{_BEDS} > div:nth-child(2) > label",
    "beds_2": f"{_BEDS} > div:nth-child(3) > label",
    "beds_3": f"{_BEDS} > div:nth-child(4) > label",
    "beds_4": f"{_BEDS} > div:nth-child(5) > label",

    # ── Filter panel — individual bathroom options ───────────────
    "baths_0": f"{_BATHS} > div:nth-child(1) > label",
    "baths_1": f"{_BATHS} > div:nth-child(2) > label",
    "baths_2": f"{_BATHS} > div:nth-child(3) > label",
    "baths_3": f"{_BATHS} > div:nth-child(4) > label",
    "baths_4": f"{_BATHS} > div:nth-child(5) > label",

    # ── Sort dropdown (on the results page, NOT inside filters) ──
    "sort_select": "p.page-title__sorting select",

    # ── Grid cards (list-view mode) ──────────────────────────────
    "grid_card": "div.listings-as-grid .grid > div",
    "card_link": ".listing-card__details > a",

    # ── Pagination ───────────────────────────────────────────────
    "next_page":      "div.listings-as-grid div.row ul > li:nth-child(4) > a",
    "next_page_last": "div.listings-as-grid div.row ul > li:last-child > a",

    # ── Detail page — floor plans ────────────────────────────────
    "plan_price": "li.unit-details__infos--price",
    "plan_baths": "li.unit-details__infos--baths",
    "plan_sqft":  "li.unit-details__infos--dimensions",
    "plan_images": "div > div > ul > li > a",

    # ── Detail page — page-level data ────────────────────────────
    "parking_span": "li.listing-card-bar__features--selectable > span",
    "utilities_items": (
        ".page-listing-details__container-bottom "
        "> div:nth-child(4) ul > li"
    ),
    "main_image": ".listing-tabbed-media img",
}

# Ordered lists for the range-click helper
_BED_KEYS  = ["beds_0", "beds_1", "beds_2", "beds_3", "beds_4"]
_BATH_KEYS = ["baths_0", "baths_1", "baths_2", "baths_3", "baths_4"]


class RentalsCaScraper(BaseScraper):
    SITE_NAME = "rentals.ca"
    BASE_URL = "https://rentals.ca"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ════════════════════════════════════════════════════════════════
    #  search_city
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL)
            self._dismiss_popups()
            self.short_delay()

            search_input = self.wait_for_element(
                By.CSS_SELECTOR, SEL["search_input"], timeout=10
            )
            search_input.clear()
            self.type_slowly(search_input, city_name)
            time.sleep(1.0)
            search_input.send_keys(Keys.RETURN)
            self.delay(self.PAGE_LOAD_DELAY)
            self._dismiss_popups()

            self._switch_to_list_view()
            self.medium_delay()

            self._apply_all_filters()
            self.medium_delay()

            self._apply_sort_recent()
            self.medium_delay()

            return self._has_listings()
        except Exception as exc:
            logger.error(f"rentals.ca search_city failed for '{city_name}': {exc}")
            import traceback; traceback.print_exc()
            return False

    # ── View toggle ──────────────────────────────────────────────

    def _switch_to_list_view(self):
        try:
            btn = self.wait_for_clickable(
                By.CSS_SELECTOR, SEL["list_view_btn"], timeout=8
            )
            self._safe_click(btn)
            self.delay(self.PAGE_LOAD_DELAY)
            logger.info("Switched to list view")
        except Exception as exc:
            logger.warning(f"Could not switch to list view: {exc}")

    # ════════════════════════════════════════════════════════════════
    #  Filter panel
    # ════════════════════════════════════════════════════════════════

    def _apply_all_filters(self):
        # ── Open panel ───────────────────────────────────────────
        try:
            btn = self.wait_for_clickable(
                By.CSS_SELECTOR, SEL["filters_button"], timeout=8
            )
            self._safe_click(btn)
        except Exception as exc:
            logger.warning(f"Could not click All Filters button: {exc}")
            return

        try:
            self.wait_for_element(
                By.CSS_SELECTOR, SEL["panel_close"], timeout=5
            )
        except TimeoutException:
            logger.warning("Filter panel did not appear")
            return

        time.sleep(0.8)

        # ── Price ────────────────────────────────────────────────
        if self.min_price:
            logger.info(f"Setting min price: ${self.min_price}")
            self._fill_filter_input(SEL["price_min"], str(self.min_price))
        if self.max_price:
            logger.info(f"Setting max price: ${self.max_price}")
            self._fill_filter_input(SEL["price_max"], str(self.max_price))

        time.sleep(0.5)

        # ── Bedrooms ─────────────────────────────────────────────
        if self.min_beds is not None:
            logger.info(f"Setting bedrooms: {self.min_beds}–{self.max_beds}")
            self._click_option_range(_BED_KEYS, self.min_beds, self.max_beds)

        time.sleep(0.5)

        # ── Bathrooms ────────────────────────────────────────────
        if self.min_baths is not None:
            min_b = int(self.min_baths)
            max_b = int(self.max_baths) if self.max_baths is not None else None
            logger.info(f"Setting bathrooms: {min_b}–{max_b}")
            self._click_option_range(_BATH_KEYS, min_b, max_b)

        time.sleep(0.5)

        # ── Close panel ──────────────────────────────────────────
        try:
            close_btn = self.wait_for_clickable(
                By.CSS_SELECTOR, SEL["panel_close"], timeout=5
            )
            self._safe_click(close_btn)
            logger.info("Closed filter panel")
        except Exception as exc:
            logger.warning(f"Could not click panel close button: {exc}")
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)

        self.delay((2.5, 4.0))

    # ── Price input helper ───────────────────────────────────────

    def _fill_filter_input(self, selector: str, value: str):
        try:
            el = self.driver.find_element(By.CSS_SELECTOR, selector)
            el.click()
            time.sleep(0.3)
            el.send_keys(Keys.CONTROL, "a")
            time.sleep(0.1)
            el.send_keys(value)
            time.sleep(0.3)
            logger.info(f"  Filled {value} into filter input")
        except Exception as exc:
            logger.warning(f"Could not fill filter input ({selector}): {exc}")

    # ── Bed / bath range clicker ─────────────────────────────────

    @staticmethod
    def _click_option_range(
        keys: List[str],
        min_val: int,
        max_val: Optional[int],
    ):
        """Click every option in [min_val, max_val].

        *keys* is an ordered list of SEL keys:
        index 0 → value 0, index 1 → value 1, …, index 4 → value 4+.
        """
        # Nothing to do — was intentionally left untyped in
        # the static signature because self isn't needed; the
        # Selenium interaction happens inside the helper below.
        pass

    # Override as instance method so we have access to self.driver:
    def _click_option_range(
        self,
        keys: List[str],
        min_val: int,
        max_val: Optional[int],
    ):
        upper = min_val if max_val is None else max_val
        # If no upper bound specified, click through to 4+
        if max_val is None:
            upper = 4

        for v in range(min_val, upper + 1):
            idx = min(v, 4)
            sel = SEL[keys[idx]]
            try:
                lbl = self.driver.find_element(By.CSS_SELECTOR, sel)
                self._safe_click(lbl)
                time.sleep(0.3)
                logger.info(f"  Clicked {keys[idx]}")
            except Exception as exc:
                logger.warning(f"  Click FAILED for {keys[idx]}: {exc}")

    # ── Sort ─────────────────────────────────────────────────────

    def _apply_sort_recent(self):
        try:
            sort_el = self.wait_for_element(
                By.CSS_SELECTOR, SEL["sort_select"], timeout=8
            )
            select = Select(sort_el)
            select.select_by_index(1)
            logger.info("Sorted by Recent")
            self.delay((2.0, 3.5))
        except Exception as exc:
            logger.warning(f"Could not set sort to Recent: {exc}")

    # ════════════════════════════════════════════════════════════════
    #  get_listings_from_page
    # ════════════════════════════════════════════════════════════════

    def _has_listings(self) -> bool:
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, SEL["card_link"], timeout=10
            )
            cards = self.find_elements_safe(By.CSS_SELECTOR, SEL["card_link"])
            logger.info(f"rentals.ca: {len(cards)} card links visible")
            return len(cards) > 0
        except TimeoutException:
            return False

    def get_listings_from_page(self) -> List[RentalListing]:
        self._scroll_grid()
        self.short_delay()

        soup = BeautifulSoup(self.get_page_source(), "lxml")
        cards = soup.select(SEL["grid_card"])
        logger.info(f"rentals.ca: {len(cards)} grid cards on page")

        stubs: List[RentalListing] = []
        for i, card in enumerate(cards):
            try:
                stub = self._stub_from_card(card)
                if stub and stub.id not in self._seen_ids:
                    self._seen_ids.add(stub.id)
                    stubs.append(stub)
                    city = self._city_from_href(stub.metadata.source_url)
                    if city:
                        self._seen_cities.add(city)
            except Exception as exc:
                logger.debug(f"Card {i} stub error: {exc}")

        logger.info(f"Collected {len(stubs)} stubs from page")
        return stubs

    def _stub_from_card(self, card: Tag) -> Optional[RentalListing]:
        link = card.select_one(SEL["card_link"])
        if not link:
            link = card.select_one("a[href]")
        if not link:
            return None

        href = link.get("href", "")
        if not href:
            return None
        url = href if href.startswith("http") else self.BASE_URL + href

        city, slug = self._parse_url_parts(url)
        readable_addr = self._slug_to_readable(slug)
        full_addr = f"{readable_addr}, {city.title()}" if city else readable_addr

        source_id = slug or str(abs(hash(url)))[:12]
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)

        return RentalListing(
            id=lid,
            address=Address(
                full_address=full_addr,
                city=city.title() if city else "",
                province="QC",
                country="Canada",
            ),
            price=PriceInfo(base_rent=0),
            features=PropertyFeatures(),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=url,
                source_id=source_id,
            ),
            title=full_addr,
        )

    def _scroll_grid(self):
        try:
            last = 0
            for _ in range(20):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(300, 600)});"
                )
                time.sleep(random.uniform(0.2, 0.45))
                cur = self.driver.execute_script("return window.pageYOffset")
                if cur == last:
                    break
                last = cur
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.4)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    #  Pagination
    # ════════════════════════════════════════════════════════════════

    def go_to_next_page(self) -> bool:
        for sel in [SEL["next_page"], SEL["next_page_last"]]:
            btn = self.find_element_safe(By.CSS_SELECTOR, sel)
            if not btn or not btn.is_displayed():
                continue
            text = btn.text.strip().lower()
            if text in ("prev", "previous", "\u2039", "\u00ab", "<"):
                continue
            cls = (btn.get_attribute("class") or "").lower()
            if "disabled" in cls:
                continue
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                self.short_delay()
                self._safe_click(btn)
                self.delay(self.PAGE_LOAD_DELAY)
                return self._has_listings()
            except Exception:
                continue

        return self._paginate_via_url()

    def _paginate_via_url(self) -> bool:
        parsed = urlparse(self.driver.current_url)
        params = parse_qs(parsed.query)

        for pname in ("p", "page", "pg"):
            if pname in params:
                cur = int(params[pname][0])
                params[pname] = [str(cur + 1)]
                break
        else:
            params["p"] = ["2"]

        new_q = urlencode(params, doseq=True)
        new_url = urlunparse(parsed._replace(query=new_q))
        old_url = self.driver.current_url

        self.navigate(new_url)
        if self.driver.current_url == old_url:
            return False
        return self._has_listings()

    # ════════════════════════════════════════════════════════════════
    #  _post_scrape — visit detail pages, extract floor plans
    # ════════════════════════════════════════════════════════════════

    def _post_scrape(self, stubs: List[RentalListing]) -> List[RentalListing]:
        if not self.fetch_details or not stubs:
            return stubs

        logger.info(
            f"\n{'='*60}\n"
            f"EXTRACTING FLOOR PLANS FROM {len(stubs)} DETAIL PAGES\n"
            f"{'='*60}"
        )

        all_listings: List[RentalListing] = []
        for i, stub in enumerate(stubs):
            logger.info(
                f"  [{i+1}/{len(stubs)}] {stub.metadata.source_url}"
            )
            try:
                plans = self._extract_detail(stub)
                if plans:
                    all_listings.extend(plans)
                    logger.info(f"    → {len(plans)} floor plan(s)")
                else:
                    all_listings.append(stub)
                    logger.info("    → 0 floor plans, keeping stub")
            except Exception as exc:
                logger.warning(f"    Detail error: {exc}")
                all_listings.append(stub)
            if i < len(stubs) - 1:
                self.delay((2.5, 5.0))

        logger.info(f"Total listings after detail extraction: {len(all_listings)}")
        return all_listings

    # ── Detail-page extraction ───────────────────────────────────

    def _extract_detail(self, stub: RentalListing) -> List[RentalListing]:
        url = stub.metadata.source_url
        self.navigate(url)
        self._dismiss_popups()
        self.short_delay()
        self._scroll_detail()

        soup = BeautifulSoup(self.get_page_source(), "lxml")

        address   = self._extract_page_address(soup, url, stub.address)
        parking   = self._extract_parking(soup)
        utilities = self._extract_utilities(soup)
        main_imgs = self._extract_main_images(soup)

        # Iterate floor-plan groups: #floor-plan-group0, …1, …2, …
        # Bedroom text lives in the sidebar at nth-child(idx + 3).
        listings: List[RentalListing] = []
        idx = 0
        while True:
            group_el = soup.select_one(f"#floor-plan-group{idx}")
            if not group_el:
                break

            sidebar_nth = idx + 3
            beds_sel = (
                f"div.listing-floor-plans "
                f"> div:nth-child({sidebar_nth}) > h3 > div"
            )
            beds  = self._parse_beds(self._text(soup, beds_sel))
            price = self._parse_price(self._text_el(group_el, SEL["plan_price"]))
            baths = self._parse_baths(self._text_el(group_el, SEL["plan_baths"]))
            sqft  = self._parse_sqft(self._text_el(group_el, SEL["plan_sqft"]))
            plan_imgs = [
                a.get("href", "")
                for a in group_el.select(SEL["plan_images"])
                if a.get("href")
            ]

            src_id = f"{stub.metadata.source_id}_fp{idx}"
            lid = RentalListing.generate_id(self.SITE_NAME, src_id, url)

            listing = RentalListing(
                id=lid,
                address=copy.deepcopy(address),
                price=PriceInfo(
                    base_rent=price or 0,
                    currency="CAD",
                    heating_included=utilities.get("heating", False),
                    water_included=utilities.get("water", False),
                    electricity_included=utilities.get("electricity", False),
                    internet_included=utilities.get("internet", False),
                ),
                features=PropertyFeatures(
                    bedrooms=beds,
                    bathrooms=baths,
                    square_feet=sqft,
                    parking_type=parking,
                    property_type=PropertyType.APARTMENT,
                ),
                amenities=Amenities(),
                metadata=ListingMetadata(
                    source_site=self.SITE_NAME,
                    source_url=url,
                    source_id=src_id,
                    photo_urls=plan_imgs or main_imgs,
                ),
                title=(
                    f"{address.full_address} – "
                    f"{'Studio' if beds == 0 else f'{beds} BR'}"
                    if beds is not None
                    else address.full_address
                ),
            )
            listings.append(listing)

            if address.city:
                self._seen_cities.add(address.city)

            idx += 1

        if not listings:
            fallback = self._fallback_listing(
                soup, url, stub, address, parking, utilities, main_imgs
            )
            if fallback:
                listings.append(fallback)

        return listings

    # ── Page-level extractors ────────────────────────────────────

    def _extract_page_address(
        self, soup: BeautifulSoup, url: str, fallback: Address
    ) -> Address:
        title_el = soup.select_one("title")
        if title_el:
            raw = title_el.get_text(strip=True)
            for sep in (" - Rentals", " | Rentals", " – Rentals", " — Rentals"):
                if sep in raw:
                    raw = raw.split(sep)[0].strip()
                    break
            if len(raw) > 5:
                city, _ = self._parse_url_parts(url)
                return Address(
                    full_address=raw,
                    city=city.title() if city else fallback.city,
                    province="QC",
                    country="Canada",
                )

        h1 = soup.select_one("h1")
        if h1:
            text = h1.get_text(" ", strip=True)
            if len(text) > 3:
                city, _ = self._parse_url_parts(url)
                return Address(
                    full_address=text,
                    city=city.title() if city else fallback.city,
                    province="QC",
                    country="Canada",
                )

        return fallback

    def _extract_parking(self, soup: BeautifulSoup) -> Optional[ParkingType]:
        el = soup.select_one(SEL["parking_span"])
        if not el:
            return None
        text = el.get_text(strip=True).lower()
        if "underground" in text:  return ParkingType.UNDERGROUND
        if "indoor" in text or "garage" in text: return ParkingType.INDOOR
        if "outdoor" in text or "surface" in text: return ParkingType.OUTDOOR
        if "street" in text:       return ParkingType.STREET
        if "no " in text or "none" in text: return ParkingType.NONE
        return ParkingType.OUTDOOR

    def _extract_utilities(self, soup: BeautifulSoup) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        for li in soup.select(SEL["utilities_items"]):
            txt = li.get_text(strip=True).lower()
            if "water" in txt:      out["water"] = True
            if "heat" in txt:       out["heating"] = True
            if "electr" in txt:     out["electricity"] = True
            if "internet" in txt or "wifi" in txt: out["internet"] = True
        return out

    def _extract_main_images(self, soup: BeautifulSoup) -> List[str]:
        imgs: List[str] = []
        for img in soup.select(SEL["main_image"]):
            src = img.get("src") or img.get("data-src") or ""
            if src and src not in imgs:
                imgs.append(src)
        return imgs[:10]

    # ── Fallback single-listing builder ──────────────────────────

    def _fallback_listing(
        self, soup, url, stub, address, parking, utilities, images,
    ) -> Optional[RentalListing]:
        lid = RentalListing.generate_id(
            self.SITE_NAME, stub.metadata.source_id, url
        )
        return RentalListing(
            id=lid,
            address=copy.deepcopy(address),
            price=PriceInfo(
                base_rent=0,
                heating_included=utilities.get("heating", False),
                water_included=utilities.get("water", False),
            ),
            features=PropertyFeatures(
                parking_type=parking,
                property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=url,
                source_id=stub.metadata.source_id,
                photo_urls=images,
            ),
            title=address.full_address,
        )

    # ── Scrolling helpers ────────────────────────────────────────

    def _scroll_detail(self):
        try:
            for _ in range(8):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(350, 650)});"
                )
                time.sleep(random.uniform(0.25, 0.5))
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    #  Parsing helpers
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _text(soup: BeautifulSoup, sel: str) -> str:
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _text_el(parent: Tag, sel: str) -> str:
        el = parent.select_one(sel)
        return el.get_text(strip=True) if el else ""

    @staticmethod
    def _parse_price(text: str) -> Optional[float]:
        if not text: return None
        m = re.search(r"\$?([\d,]+)", text.replace(" ", ""))
        return float(m.group(1).replace(",", "")) if m else None

    @staticmethod
    def _parse_beds(text: str) -> Optional[int]:
        if not text: return None
        lo = text.lower().strip()
        if "studio" in lo or "bachelor" in lo: return 0
        m = re.search(r"(\d+)", lo)
        return int(m.group(1)) if m else None

    @staticmethod
    def _parse_baths(text: str) -> Optional[float]:
        if not text: return None
        s = text.replace("\u00bd", ".5").replace("1/2", ".5")
        m = re.search(r"([\d.]+)", s)
        return float(m.group(1)) if m else None

    @staticmethod
    def _parse_sqft(text: str) -> Optional[int]:
        if not text: return None
        s = re.sub(r"[^\d]", "", text)
        return int(s) if s else None

    @staticmethod
    def _parse_url_parts(url: str):
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        city = parts[0] if parts else ""
        slug = parts[1] if len(parts) > 1 else ""
        return city, slug

    @staticmethod
    def _slug_to_readable(slug: str) -> str:
        return slug.replace("-", " ").title() if slug else ""

    @staticmethod
    def _city_from_href(url: str) -> str:
        city, _ = RentalsCaScraper._parse_url_parts(url)
        return city.title() if city else ""

    def _safe_click(self, el):
        try:
            el.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", el)