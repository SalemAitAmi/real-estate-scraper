"""
Apartments.com scraper — single selection paths, catalog-on-miss.
"""

import copy, logging, random, re, time
from typing import Dict, List, Optional
from bs4 import BeautifulSoup, Tag
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, RentalListing, RentValue,
)
from .base_scraper import (
    BaseScraper, parse_rent, parse_beds, parse_baths,
    parse_sqft, parse_price, apply_amenity_flag,
)

logger = logging.getLogger(__name__)

_SQFT_VALUES = [400,500,600,700,800,900,1000,1100,1200,1300,1400,1500,
                1600,1700,1800,1900,2000,2500,3000,3500,4000,4500]
_SQFT_INDEX = {v: i + 2 for i, v in enumerate(_SQFT_VALUES)}

_BED_CHILD  = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6}
_BATH_CHILD = {1: 2, 2: 3, 3: 4}


class ApartmentsComScraper(BaseScraper):
    SITE_NAME = "apartments.com"
    BASE_URL = "https://www.apartments.com"

    SELECTORS = {
        "search_input": ".smart-search-input[contenteditable]",
        "search_submit": (
            "#homepage-smart-search > div.smart-search-glow-container "
            "> div > div.smart-search-actions-container "
            "> button.smart-search-btn-search"),
        "autocomplete_item": "[class*='suggestion'] li",

        "filters_button": "#advancedFiltersIcon",
        "filters_panel":  "#advancedFilters",
        "apply_button":   "#seeResultBtn",

        "filter_min_rent": (
            "#advancedFilters > div > div:nth-child(2) "
            "> div.rent-price.white-bg > div "
            "> div.minRentInput > fieldset > input"),
        "filter_max_rent": (
            "#advancedFilters > div > div:nth-child(2) "
            "> div.rent-price.white-bg > div "
            "> div.maxRentInput > fieldset > input"),

        "beds_chip": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bed-filter-container > div "
            "> button:nth-child({n})"),
        "baths_chip": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bath-filter-container > div "
            "> button:nth-child({n})"),

        "sqft_min_dropdown":  "#minSF-button",
        "sqft_min_item":      "#minSF-menu > li:nth-child({n})",
        "sqft_min_item_text": "#minSF-menu > li:nth-child({n}) > div",
        "sqft_max_dropdown":  "#maxSF-button",
        "sqft_max_item":      "#maxSF-menu > li:nth-child({n})",
        "sqft_max_item_text": "#maxSF-menu > li:nth-child({n}) > div",

        "sort_dropdown":    "#sortSearchIcon",
        "sort_low_to_high": "#searchResultSortMenu > ul > li:nth-child(2)",

        "listing_card": "#placardContainer > ul > li",
        "card_link":    "article a[href]",
        "card_title":   ".property-title",
        "card_address": ".property-address",
        "card_price":   ".property-pricing",
        "card_beds":    ".bed-range",
        "card_baths":   ".bath-range",
        "card_image":   "img.lazyload",

        "pagination_container": "#paging",
        "next_page": "#paging a.next",

        # Floor-plan modal
        "fp_container": "#pricingView > div.tab-section.active",
        "fp_row":       "#pricingView > div.tab-section.active > div",
        "fp_detail_btn": (
            "#pricingView > div.tab-section.active > div:nth-child({n}) "
            "> div > div > div.column2 > div > div.actionLinksContainer "
            "> button.actionLinks.js-viewModelDetails-modal"),
        "fp_close":     "#closeRentalDetailButton",
        "fp_rent": (
            "#rentalDetailModalContentContainer > div "
            "> div.left-unit-detail-container.amenities "
            "> div.one-col > div > div.specs-header.no-wrap.pricing"),
        "fp_spec": (
            "#rentalDetailModalContentContainer > div "
            "> div.left-unit-detail-container.amenities "
            "> div.one-col > div > div:nth-child(3) > ul > li:nth-child({n})"),
        "fp_active_img": "#activeMedia",
        "fp_next_img": (
            "#rentalDetailCarouselSection > div.navigationControl "
            "> button.rightNav.js-rentalModalMediaRightNav"),
        "fp_amenity_ul": (
            "#rentalDetailModalContentContainer > div "
            "> div.left-unit-detail-container.amenities "
            "> div.amenities > ul"),

        # Detail-page contact
        "detail": {
            "contact_name":  ".propertyContactName",
            "contact_phone": "a.phoneNumber[href^='tel:']",
            "contact_email": "a.emailAddress[href^='mailto:']",
        },
    }

    # ════════════════════════════════════════════════════════════════
    #  Enrichment — expand stubs into floor-plan listings
    # ════════════════════════════════════════════════════════════════

    def enrich_listings(self, stubs):
        out = []
        for i, stub in enumerate(stubs):
            logger.info("  [%d/%d] %s", i + 1, len(stubs),
                        stub.metadata.source_url)
            try:
                plans = self._extract_property(stub)
                out.extend(plans or [stub])
                logger.info("    → %d plan(s)", len(plans) if plans else 0)
            except Exception as exc:
                logger.warning("    Detail error: %s", exc); out.append(stub)
            if i < len(stubs) - 1: self.delay((2.5, 5.0))
        logger.info("Total after enrichment: %d", len(out))
        return out

    def _extract_property(self, stub):
        self.navigate(stub.metadata.source_url)
        self.dismiss_popups(); self.short_delay(); self.scroll_page()

        if not self.css("fp_container", timeout=10):
            return self._extract_contact(stub, [stub])

        rows = self.css_all("fp_row")
        listings = []
        for idx in range(len(rows)):
            try:
                plan = self._extract_floor_plan(stub, idx)
                if plan: listings.append(plan)
            except Exception as exc:
                logger.debug("    Vacancy %d error: %s", idx, exc)
            finally:
                self._close_modal()
            time.sleep(0.6)
        return self._extract_contact(stub, listings or [stub])

    def _extract_floor_plan(self, stub, idx):
        btn = self.css("fp_detail_btn", n=idx + 1)
        if not btn: return None
        self.scroll_to_element(btn); self.safe_click(btn)
        if not self.css("fp_rent", timeout=8): return None
        time.sleep(0.5)

        soup = BeautifulSoup(self.get_page_source(), "lxml")
        rent  = parse_rent(self.soup_text(soup, "fp_rent"))
        beds  = parse_beds(self.soup_text(soup, "fp_spec", n=1))
        baths = parse_baths(self.soup_text(soup, "fp_spec", n=2))
        sqft  = parse_sqft(self.soup_text(soup, "fp_spec", n=3))
        amenity_map = self._parse_modal_amenities(soup)
        images = self._collect_modal_images()

        src_id = f"{stub.metadata.source_id}_fp{idx}"
        lid = RentalListing.generate_id(self.SITE_NAME, src_id,
                                         stub.metadata.source_url)
        listing = RentalListing(
            id=lid, address=copy.deepcopy(stub.address),
            price=PriceInfo(base_rent=rent or RentValue(amount=0), currency="CAD"),
            features=PropertyFeatures(
                bedrooms=beds, bathrooms=baths, square_feet=sqft,
                property_type=PropertyType.APARTMENT),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=stub.metadata.source_url,
                source_id=src_id, photo_urls=images))
        for cat, items in amenity_map.items():
            for item in items:
                listing.amenities.other_amenities.append(f"{cat}: {item}")
                apply_amenity_flag(listing, item)
        return listing

    def _close_modal(self):
        btn = self.css("fp_close")
        if btn and btn.is_displayed():
            try: self.safe_click(btn); time.sleep(0.4); return
            except Exception: pass
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except Exception: pass

    def _collect_modal_images(self, limit=25) -> List[str]:
        images, seen, first_src = [], set(), None
        for step in range(limit):
            img = self.css("fp_active_img")
            src = (img.get_attribute("src") or img.get_attribute("data-src") or ""
                   ) if img else ""
            if src and src not in seen:
                images.append(src); seen.add(src)
                if first_src is None: first_src = src
            elif src == first_src and step > 0: break
            nxt = self.css("fp_next_img")
            if not nxt or not nxt.is_displayed(): break
            try: self.safe_click(nxt); time.sleep(0.35)
            except Exception: break
        return images

    def _parse_modal_amenities(self, soup) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        ul = self.soup_el(soup, "fp_amenity_ul")
        if not ul: return out
        for li in ul.select(":scope > li"):
            cat_el = li.select_one(":scope > span")
            items_ul = li.select_one(":scope > ul")
            cat = cat_el.get_text(strip=True) if cat_el else "Other"
            out[cat] = ([it.get_text(strip=True)
                         for it in items_ul.select(":scope > li")
                         if it.get_text(strip=True)] if items_ul else [])
        return out

    def _extract_contact(self, stub, listings):
        """Pull contact details from page and apply to every listing."""
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        name = self.soup_text(soup, "detail.contact_name")
        phone_el = self.soup_el(soup, "detail.contact_phone")
        email_el = self.soup_el(soup, "detail.contact_email")
        phone = (phone_el.get("href", "").replace("tel:", "")
                 if phone_el else "")
        email = (email_el.get("href", "").replace("mailto:", "")
                 if email_el else "")
        for l in listings:
            if name:  l.metadata.contact_name = name
            if phone: l.metadata.contact_phone = phone
            if email: l.metadata.contact_email = email
        return listings

    # ════════════════════════════════════════════════════════════════
    #  search_city
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL)
            self.dismiss_popups(); self.short_delay()

            container = self.css("search_input", timeout=10)
            if not container: return False

            query = f"{city_name}, QC, Canada"
            ActionChains(self.driver).move_to_element(container).click().perform()
            time.sleep(0.8)
            for ch in query:
                ActionChains(self.driver).send_keys(ch).perform()
                time.sleep(random.uniform(0.06, 0.12))
            time.sleep(1.5)

            self._submit_search()
            self.delay(self.PAGE_LOAD_DELAY); self.dismiss_popups()
            self._apply_filters(); self.medium_delay()
            return self._has_listings()
        except Exception as exc:
            logger.error("apartments.com search_city failed: %s", exc)
            return False

    def _submit_search(self):
        # Pick first autocomplete suggestion
        try:
            items = self.driver.find_elements(
                By.CSS_SELECTOR, self._sel("autocomplete_item"))
            for item in items:
                if item.is_displayed() and item.text.strip():
                    self.safe_click(item)
                    logger.info("Selected autocomplete: '%s'",
                                item.text.strip()[:40])
                    return
        except Exception: pass
        # Fallback to submit button
        if not self.css_click("search_submit"):
            ActionChains(self.driver).send_keys(Keys.RETURN).perform()

    # ════════════════════════════════════════════════════════════════
    #  Filters
    # ════════════════════════════════════════════════════════════════

    def _apply_filters(self):
        if not self.css_click("filters_button"): return
        if not self.css("filters_panel", timeout=5): return
        time.sleep(0.8)

        if self.min_price: self._fill_input("filter_min_rent", str(self.min_price))
        if self.max_price: self._fill_input("filter_max_rent", str(self.max_price))
        time.sleep(0.5)

        self._set_beds(); time.sleep(0.5)
        self._set_baths(); time.sleep(0.5)
        self._set_sqft(); time.sleep(0.5)

        if not self.css_click("apply_button"):
            self.css_click("filters_button")
        self.delay((2.0, 4.0))

    def _fill_input(self, name: str, value: str):
        el = self.css(name)
        if not el: return
        el.click(); time.sleep(0.2)
        el.send_keys(Keys.CONTROL, "a"); el.send_keys(value)
        el.send_keys(Keys.TAB); time.sleep(0.3)

    def _set_beds(self):
        if self.min_beds is None: return
        exact = self.max_beds is not None and self.min_beds == self.max_beds
        if self.min_beds == 0 and (exact or self.max_beds == 0):
            self.css_click("beds_chip", n=_BED_CHILD[0]); return
        n = _BED_CHILD.get(min(self.min_beds, 4), _BED_CHILD[4])
        self.css_click("beds_chip", n=n); time.sleep(0.4)
        if exact: self.css_click("beds_chip", n=n)

    def _set_baths(self):
        if self.min_baths is None: return
        b = int(self.min_baths)
        if b < 1: return
        exact = self.max_baths is not None and int(self.max_baths) == b
        n = _BATH_CHILD.get(min(b, 3), _BATH_CHILD[3])
        self.css_click("baths_chip", n=n); time.sleep(0.4)
        if exact: self.css_click("baths_chip", n=n)

    def _set_sqft(self):
        for which, val in [("min", self.min_sqft), ("max", self.max_sqft)]:
            if not val: continue
            closest = min(_SQFT_VALUES, key=lambda v: abs(v - val))
            n = _SQFT_INDEX[closest]
            if not self.css_click(f"sqft_{which}_dropdown"): continue
            time.sleep(0.6)
            self.css_click(f"sqft_{which}_item", n=n); time.sleep(0.4)

    # ════════════════════════════════════════════════════════════════
    #  Extraction
    # ════════════════════════════════════════════════════════════════

    def _has_listings(self) -> bool:
        el = self.css("listing_card", timeout=10)
        return el is not None

    def get_listings_from_page(self) -> List[RentalListing]:
        self.scroll_page(); self.short_delay()
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        cards = self.soup_all(soup, "listing_card")
        listings = []
        for i, card in enumerate(cards):
            try:
                listing = self._parse_card(card)
                if listing:
                    listings.append(listing)
                    if listing.address.city:
                        self._seen_cities.add(listing.address.city)
            except Exception as exc:
                logger.debug("Card %d parse error: %s", i, exc)
        logger.info("Extracted %d listings", len(listings))
        return listings

    def _parse_card(self, card: Tag) -> Optional[RentalListing]:
        link_el = card.select_one(self._sel("card_link"))
        if not link_el: return None
        href = link_el.get("href", "")
        url = href if href.startswith("http") else self.BASE_URL + href
        if not url: return None

        addr_text = self._card_text(card, "card_address") or self._card_text(card, "card_title")
        if not addr_text: return None

        price_text = self._card_text(card, "card_price")
        beds_text  = self._card_text(card, "card_beds")
        baths_text = self._card_text(card, "card_baths")

        base_rent = parse_price(price_text)
        beds  = parse_beds(beds_text)
        baths = parse_baths(baths_text)

        img_el = card.select_one(self._sel("card_image"))
        img_url = ((img_el.get("data-src") or img_el.get("src") or "")
                   if img_el else "")

        source_id = (link_el or card).get("data-listingid", "") or self._source_id(url)
        city, province = self._parse_city_province(addr_text)
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)

        return RentalListing(
            id=lid,
            address=Address(full_address=addr_text, city=city,
                            province=province, country="Canada"),
            price=PriceInfo(base_rent=RentValue(amount=base_rent or 0),
                            currency="CAD"),
            features=PropertyFeatures(bedrooms=beds, bathrooms=baths,
                                      property_type=PropertyType.APARTMENT),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME, source_url=url,
                source_id=source_id,
                photo_urls=[img_url] if img_url else []))

    def _card_text(self, card: Tag, name: str) -> str:
        el = card.select_one(self._sel(name))
        return el.get_text(" ", strip=True) if el else ""

    # ════════════════════════════════════════════════════════════════
    #  Pagination
    # ════════════════════════════════════════════════════════════════

    def go_to_next_page(self) -> bool:
        try:
            btn = self.css("next_page")
            if not btn or not btn.is_displayed(): return False
            if "disabled" in (btn.get_attribute("class") or "").lower():
                return False
            old_url = self.driver.current_url
            self.safe_click(btn); self.delay(self.PAGE_LOAD_DELAY)
            return self.driver.current_url != old_url or self._has_listings()
        except Exception:
            return False

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _source_id(url: str) -> str:
        m = re.search(r"/(\d{5,})", url)
        if m: return m.group(1)
        parts = url.rstrip("/").split("/")
        return parts[-1][:20] if parts else str(abs(hash(url)))[:12]

    @staticmethod
    def _parse_city_province(addr_text: str):
        parts = [p.strip() for p in addr_text.split(",") if p.strip()]
        province, city = "QC", ""
        if len(parts) >= 2:
            last = parts[-1].strip().upper()
            m = re.search(r"\b([A-Z]{2})\b", last)
            if m: province = m.group(1)
            city = parts[-2] if len(parts) >= 2 else parts[0]
        elif parts:
            city = parts[0]
        return city, province