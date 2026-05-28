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
    ElementNotInteractableException,
)

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, ParkingType,
    LaundryType, RentalListing, RentValue,
)
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
#  Selectors
# ────────────────────────────────────────────────────────────────────

_PANEL = (
    "body > div.filters-drawer-backdrop > div "
    "> div.page-panel.listing-filters-panel "
    "> div.page-panel__content-container > div"
)

_BEDS = (
    f"{_PANEL} > div.listing-filters-panel__card-container"
    ".listing-filters-panel__card-container--pair "
    "> div:nth-child(1) > div > div"
)

_BATHS = (
    f"{_PANEL} > div.listing-filters-panel__card-container"
    ".listing-filters-panel__card-container--pair "
    "> div:nth-child(2) > div > div"
)

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
    "price_min": (
        f"{_PANEL} > div:nth-child(3) > div > div "
        "> div.number-range.mt-1 > div.number-range__inputs "
        "> div:nth-child(1) > input"
    ),
    "price_max": (
        f"{_PANEL} > div:nth-child(3) > div > div "
        "> div.number-range.mt-1 > div.number-range__inputs "
        "> div:nth-child(3) > input"
    ),

    # ── Filter panel — bedroom checkboxes ────────────────────────
    #    0 = Studio, 1, 2, 3, 4+
    "beds_0": f"{_BEDS} > div:nth-child(1) > label > input[type=checkbox]",
    "beds_1": f"{_BEDS} > div:nth-child(2) > label > input[type=checkbox]",
    "beds_2": f"{_BEDS} > div:nth-child(3) > label > input[type=checkbox]",
    "beds_3": f"{_BEDS} > div:nth-child(4) > label > input[type=checkbox]",
    "beds_4": f"{_BEDS} > div:nth-child(5) > label > input[type=checkbox]",

    # ── Filter panel — bathroom checkboxes ───────────────────────
    "baths_1": f"{_BATHS} > div:nth-child(1) > label > input[type=checkbox]",
    "baths_2": f"{_BATHS} > div:nth-child(2) > label > input[type=checkbox]",
    "baths_3": f"{_BATHS} > div:nth-child(3) > label > input[type=checkbox]",
    "baths_4": f"{_BATHS} > div:nth-child(4) > label > input[type=checkbox]",
                 
    # ── Sort dropdown (on the results page, NOT inside filters) ──
    "sort_select": (
        "#app > div > div > div.page-search-results__grid "
        "> div.listings-as-grid > div.header > div "
        "> div.page-title__bottom-line > p.page-title__sorting "
        "> div > select"
    ),

    # ── Grid cards (list-view mode) ──────────────────────────────
    "grid_card": "div.listings-as-grid .grid > div",
    "card_link": ".listing-card__details > a",

    # ── Pagination ───────────────────────────────────────────────
    "next_page": (
    "#app > div > div > div.page-search-results__grid "
    "> div.listings-as-grid > div.row > div > div > div > div "
    "> ul > li:last-child > a"
),

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

_BED_KEY_BY_VALUE: Dict[int, str] = {
    0: "beds_0", 1: "beds_1", 2: "beds_2", 3: "beds_3", 4: "beds_4",
}
_BATH_KEY_BY_VALUE: Dict[int, str] = {
    1: "baths_1", 2: "baths_2", 3: "baths_3", 4: "baths_4",
}


class RentalsCaScraper(BaseScraper):
    SITE_NAME = "rentals.ca"
    BASE_URL = "https://rentals.ca"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ════════════════════════════════════════════════════════════════
    #  Detail enrichment (Model A)
    # ════════════════════════════════════════════════════════════════

    def enrich_listings(self, stubs: List[RentalListing]) -> List[RentalListing]:
        if not stubs:
            return stubs

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
            self._click_option_range(
                _BED_KEY_BY_VALUE, self.min_beds, self.max_beds
            )

        time.sleep(0.5)

        # ── Bathrooms ────────────────────────────────────────────
        if self.min_baths is not None and int(self.min_baths) >= 1:
            min_b = int(self.min_baths)
            max_b = int(self.max_baths) if self.max_baths is not None else None
            logger.info(f"Setting bathrooms: {min_b}–{max_b}")
            self._click_option_range(_BATH_KEY_BY_VALUE, min_b, max_b)

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
            logger.warning(f"Could not fill filter input ({selector[:60]}…): {exc}")

    # ── Bed / bath range clicker ─────────────────────────────────

    def _click_option_range(
        self,
        key_map: Dict[int, str],
        min_val: int,
        max_val: Optional[int],
    ):
        if not key_map:
            return
        min_key = min(key_map.keys())
        max_key = max(key_map.keys())

        upper = max_val if max_val is not None else max_key

        for v in range(min_val, upper + 1):
            if v < min_key:
                continue
            v_clamped = min(v, max_key)
            sel = SEL[key_map[v_clamped]]
            try:
                cb = self.driver.find_element(By.CSS_SELECTOR, sel)
                if cb.is_selected():
                    logger.info(f"  {key_map[v_clamped]} already checked — skipping")
                    continue
                self.driver.execute_script("arguments[0].click();", cb)
                time.sleep(0.3)
                logger.info(f"  Clicked {key_map[v_clamped]}")
            except Exception as exc:
                logger.warning(f"  Click FAILED for {key_map[v_clamped]}: {exc}")

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

    # FIX: removed _seen_ids gate so every card on every page is
    #      returned as a stub.  Cross-page duplicates (featured /
    #      promoted cards that repeat) are handled later by the
    #      pipeline's deduplicate_listings() pass.
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
                if stub:
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
            price=PriceInfo(base_rent=RentValue(amount=0)),
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
        try:
            btn = self.find_element_safe(By.CSS_SELECTOR, SEL["next_page"])
            if not btn or not btn.is_displayed():
                return self._paginate_via_url()
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
            self.short_delay()
            self._safe_click(btn)
            self.delay(self.PAGE_LOAD_DELAY)
            return self._has_listings()
        except Exception:
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
    #  Detail-page extraction (called by enrich_listings)
    # ════════════════════════════════════════════════════════════════

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
                    base_rent=RentValue(amount=price or 0),
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
                base_rent=RentValue(amount=0),
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