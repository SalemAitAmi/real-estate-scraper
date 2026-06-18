"""
Rentals.ca scraper — single selection paths, catalog-on-miss.
"""

import copy, logging, random, re, time
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup, Tag
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, ParkingType,
    RentalListing, RentValue,
)
from .base_scraper import (
    BaseScraper, parse_price, parse_beds, parse_baths,
    parse_sqft, parse_parking_type,
)

logger = logging.getLogger(__name__)

_PANEL = (
    "body > div.filters-drawer-backdrop > div "
    "> div.page-panel.listing-filters-panel "
    "> div.page-panel__content-container > div")
_BEDS = (
    f"{_PANEL} > div.listing-filters-panel__card-container"
    ".listing-filters-panel__card-container--pair "
    "> div:nth-child(1) > div > div")
_BATHS = (
    f"{_PANEL} > div.listing-filters-panel__card-container"
    ".listing-filters-panel__card-container--pair "
    "> div:nth-child(2) > div > div")

_BED_KEY  = {0: "beds_0", 1: "beds_1", 2: "beds_2", 3: "beds_3", 4: "beds_4"}
_BATH_KEY = {1: "baths_1", 2: "baths_2", 3: "baths_3", 4: "baths_4"}


class RentalsCaScraper(BaseScraper):
    SITE_NAME = "rentals.ca"
    BASE_URL = "https://rentals.ca"

    SELECTORS = {
        "search_input":   ".page-home__hero-search .search-input input",
        "search_button":  ".page-home__hero-search .search-input button",
        "list_view_btn":  ".place-view-selector a.first",
        "filters_button": "div.filters-bar.d-none.d-lg-flex > button",
        "panel_close":    ".page-panel__title-container button",

        "price_min": (
            f"{_PANEL} > div:nth-child(3) > div > div "
            "> div.number-range.mt-1 > div.number-range__inputs "
            "> div:nth-child(1) > input"),
        "price_max": (
            f"{_PANEL} > div:nth-child(3) > div > div "
            "> div.number-range.mt-1 > div.number-range__inputs "
            "> div:nth-child(3) > input"),

        "beds_0": f"{_BEDS} > div:nth-child(1) > label > input[type=checkbox]",
        "beds_1": f"{_BEDS} > div:nth-child(2) > label > input[type=checkbox]",
        "beds_2": f"{_BEDS} > div:nth-child(3) > label > input[type=checkbox]",
        "beds_3": f"{_BEDS} > div:nth-child(4) > label > input[type=checkbox]",
        "beds_4": f"{_BEDS} > div:nth-child(5) > label > input[type=checkbox]",

        "baths_1": f"{_BATHS} > div:nth-child(1) > label > input[type=checkbox]",
        "baths_2": f"{_BATHS} > div:nth-child(2) > label > input[type=checkbox]",
        "baths_3": f"{_BATHS} > div:nth-child(3) > label > input[type=checkbox]",
        "baths_4": f"{_BATHS} > div:nth-child(4) > label > input[type=checkbox]",

        "sort_select": (
            "#app > div > div > div.page-search-results__grid "
            "> div.listings-as-grid > div.header > div "
            "> div.page-title__bottom-line > p.page-title__sorting "
            "> div > select"),

        "grid_card": "div.listings-as-grid .grid > div",
        "card_link": ".listing-card__details > a",

        "next_page": (
            "#app > div > div > div.page-search-results__grid "
            "> div.listings-as-grid > div.row > div > div > div > div "
            "> ul > li:last-child > a"),

        # Detail page
        "plan_price":  "li.unit-details__infos--price",
        "plan_baths":  "li.unit-details__infos--baths",
        "plan_sqft":   "li.unit-details__infos--dimensions",
        "plan_images": "div > div > ul > li > a",
        "beds_heading": "div.listing-floor-plans > div:nth-child({n}) > h3 > div",

        "parking_span":    "li.listing-card-bar__features--selectable > span",
        "utilities_items": (
            ".page-listing-details__container-bottom "
            "> div:nth-child(4) ul > li"),
        "main_image":      ".listing-tabbed-media img",

        "detail": {
            "contact_phone": ".listing-contact a[href^='tel:']",
            "contact_email": ".listing-contact a[href^='mailto:']",
            "contact_name":  ".listing-contact .contact-name",
        },
    }

    # ════════════════════════════════════════════════════════════════
    #  Enrichment
    # ════════════════════════════════════════════════════════════════

    def enrich_listings(self, stubs):
        if not stubs: return stubs
        out: List[RentalListing] = []
        for i, stub in enumerate(stubs):
            logger.info("  [%d/%d] %s", i + 1, len(stubs),
                        stub.metadata.source_url)
            try:
                plans = self._extract_detail(stub)
                out.extend(plans or [stub])
                logger.info("    → %d plan(s)", len(plans) if plans else 0)
            except Exception as exc:
                logger.warning("    Detail error: %s", exc); out.append(stub)
            if i < len(stubs) - 1: self.delay((2.5, 5.0))
        logger.info("Total after detail: %d", len(out))
        return out

    def _extract_detail(self, stub) -> List[RentalListing]:
        url = stub.metadata.source_url
        self.navigate(url); self.dismiss_popups(); self.short_delay()
        self.scroll_page(steps=8)

        soup = BeautifulSoup(self.get_page_source(), "lxml")
        address   = self._page_address(soup, url, stub.address)
        parking   = self._page_parking(soup)
        utilities = self._page_utilities(soup)
        main_imgs = self._page_images(soup)
        contact   = self._page_contact(soup)

        listings: List[RentalListing] = []
        idx = 0
        while True:
            group = soup.select_one(f"#floor-plan-group{idx}")
            if not group: break

            beds  = parse_beds(self.soup_text(soup, "beds_heading", n=idx + 3))
            price = parse_price(self._el_text(group, "plan_price"))
            baths = parse_baths(self._el_text(group, "plan_baths"))
            sqft  = parse_sqft(self._el_text(group, "plan_sqft"))
            imgs  = [a.get("href", "")
                     for a in group.select(self._sel("plan_images"))
                     if a.get("href")]

            src_id = f"{stub.metadata.source_id}_fp{idx}"
            lid = RentalListing.generate_id(self.SITE_NAME, src_id, url)
            listings.append(RentalListing(
                id=lid, address=copy.deepcopy(address),
                price=PriceInfo(
                    base_rent=RentValue(amount=price or 0), currency="CAD",
                    heating_included=utilities.get("heating", False),
                    water_included=utilities.get("water", False),
                    electricity_included=utilities.get("electricity", False),
                    internet_included=utilities.get("internet", False)),
                features=PropertyFeatures(
                    bedrooms=beds, bathrooms=baths, square_feet=sqft,
                    parking_type=parking,
                    property_type=PropertyType.APARTMENT),
                amenities=Amenities(),
                metadata=ListingMetadata(
                    source_site=self.SITE_NAME, source_url=url,
                    source_id=src_id, photo_urls=imgs or main_imgs,
                    **contact)))
            if address.city: self._seen_cities.add(address.city)
            idx += 1

        if not listings:
            lid = RentalListing.generate_id(
                self.SITE_NAME, stub.metadata.source_id, url)
            listings.append(RentalListing(
                id=lid, address=copy.deepcopy(address),
                price=PriceInfo(
                    base_rent=RentValue(amount=0),
                    heating_included=utilities.get("heating", False),
                    water_included=utilities.get("water", False)),
                features=PropertyFeatures(
                    parking_type=parking,
                    property_type=PropertyType.APARTMENT),
                amenities=Amenities(),
                metadata=ListingMetadata(
                    source_site=self.SITE_NAME, source_url=url,
                    source_id=stub.metadata.source_id,
                    photo_urls=main_imgs, **contact)))
        return listings

    def _el_text(self, parent: Tag, name: str) -> str:
        el = parent.select_one(self._sel(name))
        return el.get_text(strip=True) if el else ""

    # ── Page-level extractors ────────────────────────────────────

    def _page_address(self, soup, url, fallback):
        title_el = soup.select_one("title")
        if title_el:
            raw = title_el.get_text(strip=True)
            for sep in (" - Rentals", " | Rentals", " – Rentals", " — Rentals"):
                if sep in raw: raw = raw.split(sep)[0].strip(); break
            if len(raw) > 5:
                city, _ = self._url_parts(url)
                return Address(full_address=raw,
                               city=city.title() if city else fallback.city,
                               province="QC", country="Canada")
        h1 = soup.select_one("h1")
        if h1 and len(h1.get_text(" ", strip=True)) > 3:
            city, _ = self._url_parts(url)
            return Address(full_address=h1.get_text(" ", strip=True),
                           city=city.title() if city else fallback.city,
                           province="QC", country="Canada")
        return fallback

    def _page_parking(self, soup) -> Optional[ParkingType]:
        el = self.soup_el(soup, "parking_span")
        return parse_parking_type(el.get_text(strip=True)) if el else None

    def _page_utilities(self, soup) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        for li in self.soup_all(soup, "utilities_items"):
            t = li.get_text(strip=True).lower()
            if "water" in t: out["water"] = True
            if "heat" in t:  out["heating"] = True
            if "electr" in t: out["electricity"] = True
            if "internet" in t or "wifi" in t: out["internet"] = True
        return out

    def _page_images(self, soup) -> List[str]:
        imgs = []
        for img in self.soup_all(soup, "main_image"):
            src = img.get("src") or img.get("data-src") or ""
            if src and src not in imgs: imgs.append(src)
        return imgs[:10]

    def _page_contact(self, soup) -> Dict[str, Optional[str]]:
        out: Dict[str, Optional[str]] = {}
        name = self.soup_text(soup, "detail.contact_name")
        if name: out["contact_name"] = name
        phone_el = self.soup_el(soup, "detail.contact_phone")
        if phone_el:
            out["contact_phone"] = phone_el.get("href", "").replace("tel:", "")
        email_el = self.soup_el(soup, "detail.contact_email")
        if email_el:
            out["contact_email"] = email_el.get("href", "").replace("mailto:", "")
        return out

    # ════════════════════════════════════════════════════════════════
    #  search_city
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL); self.dismiss_popups(); self.short_delay()
            si = self.css("search_input", timeout=10)
            if not si: return False
            si.clear(); self.type_slowly(si, city_name)
            time.sleep(1.0); si.send_keys(Keys.RETURN)
            self.delay(self.PAGE_LOAD_DELAY); self.dismiss_popups()

            self.css_click("list_view_btn", timeout=8)
            self.delay(self.PAGE_LOAD_DELAY); self.medium_delay()
            self._apply_filters(); self.medium_delay()
            self._apply_sort(); self.medium_delay()
            return self._has_listings()
        except Exception as exc:
            logger.error("rentals.ca search failed for '%s': %s", city_name, exc)
            return False

    # ════════════════════════════════════════════════════════════════
    #  Filters
    # ════════════════════════════════════════════════════════════════

    def _apply_filters(self):
        if not self.css_click("filters_button", timeout=8): return
        if not self.css("panel_close", timeout=5): return
        time.sleep(0.8)

        if self.min_price: self._fill("price_min", str(self.min_price))
        if self.max_price: self._fill("price_max", str(self.max_price))
        time.sleep(0.5)

        if self.min_beds is not None:
            self._check_range(_BED_KEY, self.min_beds, self.max_beds)
        time.sleep(0.5)
        if self.min_baths is not None and int(self.min_baths) >= 1:
            self._check_range(
                _BATH_KEY, int(self.min_baths),
                int(self.max_baths) if self.max_baths else None)
        time.sleep(0.5)

        if not self.css_click("panel_close", timeout=5):
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        self.delay((2.5, 4.0))

    def _fill(self, name: str, value: str):
        el = self.css(name)
        if not el: return
        el.click(); time.sleep(0.3)
        el.send_keys(Keys.CONTROL, "a"); el.send_keys(value); time.sleep(0.3)

    def _check_range(self, key_map, min_v, max_v):
        lo, hi = min(key_map), max(key_map)
        upper = max_v if max_v is not None else hi
        for v in range(max(min_v, lo), min(upper, hi) + 1):
            sel_name = key_map.get(v)
            if not sel_name: continue
            cb = self.css(sel_name)
            if not cb: continue
            if cb.is_selected(): continue
            self.driver.execute_script("arguments[0].click();", cb)
            time.sleep(0.3)

    def _apply_sort(self):
        el = self.css("sort_select", timeout=8)
        if not el: return
        try:
            Select(el).select_by_index(1)
            logger.info("Sorted by Recent"); self.delay((2.0, 3.5))
        except Exception as exc:
            logger.warning("Sort failed: %s", exc)

    # ════════════════════════════════════════════════════════════════
    #  Extraction
    # ════════════════════════════════════════════════════════════════

    def _has_listings(self) -> bool:
        return self.css("card_link", timeout=10) is not None

    def get_listings_from_page(self) -> List[RentalListing]:
        self.scroll_page(steps=20); self.short_delay()
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        cards = self.soup_all(soup, "grid_card")
        stubs: List[RentalListing] = []
        for i, card in enumerate(cards):
            try:
                stub = self._card_to_stub(card)
                if stub:
                    stubs.append(stub)
                    city = self._city_from_url(stub.metadata.source_url)
                    if city: self._seen_cities.add(city)
            except Exception as exc:
                logger.debug("Card %d error: %s", i, exc)
        logger.info("Collected %d stubs", len(stubs))
        return stubs

    def _card_to_stub(self, card: Tag) -> Optional[RentalListing]:
        link = card.select_one(self._sel("card_link"))
        if not link: link = card.select_one("a[href]")
        if not link: return None
        href = link.get("href", "")
        if not href: return None
        url = href if href.startswith("http") else self.BASE_URL + href
        city, slug = self._url_parts(url)
        readable = slug.replace("-", " ").title() if slug else ""
        full_addr = f"{readable}, {city.title()}" if city else readable
        source_id = slug or str(abs(hash(url)))[:12]
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)
        return RentalListing(
            id=lid,
            address=Address(full_address=full_addr,
                            city=city.title() if city else "",
                            province="QC", country="Canada"),
            price=PriceInfo(base_rent=RentValue(amount=0)),
            features=PropertyFeatures(),
            amenities=Amenities(),
            metadata=ListingMetadata(source_site=self.SITE_NAME,
                                     source_url=url, source_id=source_id))

    # ════════════════════════════════════════════════════════════════
    #  Pagination
    # ════════════════════════════════════════════════════════════════

    def go_to_next_page(self) -> bool:
        try:
            btn = self.css("next_page")
            if btn and btn.is_displayed():
                self.scroll_to_element(btn); self.safe_click(btn)
                self.delay(self.PAGE_LOAD_DELAY)
                return self._has_listings()
        except Exception: pass
        return self._url_paginate()

    def _url_paginate(self) -> bool:
        parsed = urlparse(self.driver.current_url)
        params = parse_qs(parsed.query)
        for p in ("p", "page", "pg"):
            if p in params:
                params[p] = [str(int(params[p][0]) + 1)]; break
        else:
            params["p"] = ["2"]
        new_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
        old = self.driver.current_url
        self.navigate(new_url)
        return self.driver.current_url != old and self._has_listings()

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _url_parts(url):
        parts = urlparse(url).path.strip("/").split("/")
        return (parts[0] if parts else "",
                parts[1] if len(parts) > 1 else "")

    @staticmethod
    def _city_from_url(url):
        city, _ = RentalsCaScraper._url_parts(url)
        return city.title() if city else ""