"""
Apartments.com scraper — Selenium + undetected_chromedriver.

Selector status
───────────────
• Homepage search, advanced-filter panel, beds/baths chips, pet
  policy, square-footage, sort, and apply button are VERIFIED.
• Results-page listing cards, card internals, pagination, and
  detail-page selectors are UNVERIFIED best-effort baselines.
"""

import logging
import random
import re
import time
from typing import Dict, List, Optional, Any
from datetime import datetime

from bs4 import BeautifulSoup, Tag
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
)

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, HeatingType, ParkingType,
    LaundryType, RentalListing,
)
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
#  Predefined square-footage menu values (positions 2–23)
# ────────────────────────────────────────────────────────────────────
_SQFT_VALUES: List[int] = [
    400, 500, 600, 700, 800, 900,
    1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900,
    2000, 2500, 3000, 3500, 4000, 4500,
]
_SQFT_INDEX: Dict[int, int] = {v: i + 2 for i, v in enumerate(_SQFT_VALUES)}


class ApartmentsComScraper(BaseScraper):
    SITE_NAME = "apartments.com"
    BASE_URL = "https://www.apartments.com"

    # ────────────────────────────────────────────────────────────
    #  Selectors
    # ────────────────────────────────────────────────────────────

    SELECTORS = {
        # ── Homepage search (smart search — NO child <input>) ────
        "search_input": "#homepage-smart-search > div.smart-search-glow-container > div > div.smart-search-input.grow[contenteditable]",
        
        "search_submit": (
            "#homepage-smart-search "
            "> div.smart-search-glow-container "
            "> div > div.smart-search-actions-container "
            "> button.smart-search-btn-search"
        ),

        # ── Advanced-filter panel ────────────────────────────────
        "filters_button": "#advancedFiltersIcon",
        "filters_panel":  "#advancedFilters",
        "apply_button":   "#seeResultBtn",

        # ── Price inputs (inside #advancedFilters) ───────────────
        "filter_min_rent": (
            "#advancedFilters > div > div:nth-child(2) "
            "> div.rent-price.white-bg > div "
            "> div.minRentInput > fieldset > input"
        ),
        "filter_max_rent": (
            "#advancedFilters > div > div:nth-child(2) "
            "> div.rent-price.white-bg > div "
            "> div.maxRentInput > fieldset > input"
        ),

        # ── Beds chips (template — use .format(n=…)) ────────────
        "beds_chip": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bed-filter-container > div "
            "> button:nth-child({n})"
        ),
        "beds_selected": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bed-filter-container > div "
            "> button.button-group-item.bed-filter-button.highlighted"
        ),

        # ── Baths chips (template) ──────────────────────────────
        "baths_chip": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bath-filter-container > div "
            "> button:nth-child({n})"
        ),
        "baths_selected": (
            "#advancedFilters > div "
            "> div.advancedFilterSection.bed-bath-filters-section "
            "> div > div.button-group.bath-filter-container > div "
            "> button.button-group-item.bath-filter-button.highlighted"
        ),

        # ── Home-type checkboxes (not currently used) ────────────
        "home_type_1":  "#PropertyType-1",
        "home_type_2":  "#PropertyType-2",
        "home_type_4":  "#PropertyType-4",
        "home_type_16": "#PropertyType-16",

        # ── Pet policy ───────────────────────────────────────────
        "pet_policy_dropdown": "#adv-petPolicy-select-button",
        "pet_dog":  "#adv-petPolicy-select-menu > li:nth-child(1)",
        "pet_cat":  "#adv-petPolicy-select-menu > li:nth-child(2)",
        "pet_both": "#adv-petPolicy-select-menu > li:nth-child(3)",

        # ── Square-footage drop-downs (templates) ────────────────
        "sqft_min_dropdown":  "#minSF-button",
        "sqft_min_item":      "#minSF-menu > li:nth-child({n})",
        "sqft_min_item_text": "#minSF-menu > li:nth-child({n}) > div",
        "sqft_max_dropdown":  "#maxSF-button",
        "sqft_max_item":      "#maxSF-menu > li:nth-child({n})",
        "sqft_max_item_text": "#maxSF-menu > li:nth-child({n}) > div",

        # ── Sort (click icon → pick from menu) ──────────────────
        "sort_dropdown":     "#sortSearchIcon",
        "sort_low_to_high":  "#searchResultSortMenu > ul > li:nth-child(2)",
        "sort_high_to_low":  "#searchResultSortMenu > ul > li:nth-child(3)",

        # ── Listing cards (UNVERIFIED) ───────────────────────────
        "listing_card": "#placardContainer > ul > li",
        "card_link": (
            ".property-info a, "
            "a.property-link, "
            "a[data-listingid], "
            "article a[href]"
        ),
        "card_title": (
            ".property-title, "
            ".js-placardTitle, "
            "span.title"
        ),
        "card_address": (
            ".property-address, "
            ".property-information .location"
        ),
        "card_price": (
            ".property-pricing, "
            ".price-range, "
            ".rent-range"
        ),
        "card_beds": ".bed-range, .property-beds",
        "card_image": "img.lazyload, img[data-src], .property-image img",

        # ── Pagination (UNVERIFIED) ──────────────────────────────
        "pagination_container": "#paging",
        "next_page": (
            "#paging a.next, "
            "#paging > ol > li:last-child > a"
        ),

        # ── Detail page (UNVERIFIED) ─────────────────────────────
        "detail": {
            "description": ".descriptionSection p, #TextDescription",
            "amenity_items": ".amenityCard li, .amenitiesSection li",
            "fee_items": ".feesSection li, .pricingSection li",
            "pet_section": ".petSection, [data-testid='pet-policy']",
            "contact_name": ".contactName, .managerName",
            "contact_phone": "a[href^='tel:']",
        },
    }

    _BED_CHILD  = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6}
    _BATH_CHILD = {1: 2, 2: 3, 3: 4}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ── Detail enrichment (Model A) ──────────────────────────────

    def enrich_listings(self, listings):
        if not listings:
            return listings
        return self._enrich_all(listings)

    def _enrich_all(self, listings):
        enriched = []
        for i, listing in enumerate(listings):
            logger.info(
                f"  [{i+1}/{len(listings)}] {listing.address.full_address[:50]}…"
            )
            try:
                enriched.append(self._fetch_detail(listing))
            except Exception as exc:
                logger.warning(f"Detail error {listing.id}: {exc}")
                enriched.append(listing)
            if i < len(listings) - 1:
                self.delay((2.5, 5.0))
        return enriched

    def _fetch_detail(self, listing):
        url = listing.metadata.source_url
        if not url:
            return listing
        self.navigate(url)
        self._dismiss_popups()
        self.short_delay()
        self._scroll_page()
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        sel = self.SELECTORS["detail"]

        desc_el = soup.select_one(sel["description"])
        if desc_el:
            listing.description = desc_el.get_text(" ", strip=True)

        amenity_texts = []
        for li in soup.select(sel["amenity_items"]):
            txt = li.get_text(strip=True)
            amenity_texts.append(txt)
            self._apply_amenity(listing, txt.lower())
        if amenity_texts:
            listing.amenities.other_amenities = amenity_texts

        for li in soup.select(sel["fee_items"]):
            txt = li.get_text(strip=True).lower()
            if "park" in txt:
                m = re.search(r"\$?([\d,]+)", txt)
                if m:
                    listing.price.parking_fee = float(m.group(1).replace(",", ""))
            if "deposit" in txt:
                m = re.search(r"\$?([\d,]+)", txt)
                if m:
                    listing.price.security_deposit = float(
                        m.group(1).replace(",", "")
                    )

        pet_el = soup.select_one(sel["pet_section"])
        if pet_el:
            pt = pet_el.get_text(strip=True).lower()
            listing.features.pets_allowed = "allowed" in pt or "friendly" in pt
            listing.features.cats_allowed = "cat" in pt and "no cat" not in pt
            listing.features.dogs_allowed = "dog" in pt and "no dog" not in pt

        cn = soup.select_one(sel["contact_name"])
        if cn:
            listing.metadata.contact_name = cn.get_text(strip=True)
        cp = soup.select_one(sel["contact_phone"])
        if cp:
            listing.metadata.contact_phone = cp.get_text(strip=True)

        return listing

    # ════════════════════════════════════════════════════════════════
    #  search_city
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL)
            self._dismiss_popups()
            self.short_delay()

            query = f"{city_name}, QC, Canada"

            # ── Locate the smart-search container ────────────────
            container = self.SELECTORS["search_input"]
            if not container:
                logger.error("apartments.com: search container not found")
                return False

            # ── Type into it ─────────────────────────────────────
            if not self._type_into_smart_search(container, query):
                logger.error("apartments.com: could not type search query")
                return False

            time.sleep(1.5)

            # ── Submit ───────────────────────────────────────────
            self._submit_smart_search(container)

            self.delay(self.PAGE_LOAD_DELAY)
            self._dismiss_popups()

            # ── Filters on results page ──────────────────────────
            self._apply_filters()
            self.medium_delay()

            return self._has_listings()
        except Exception as exc:
            logger.error(f"apartments.com search_city failed: {exc}")
            import traceback; traceback.print_exc()
            return False

    # ── Smart-search typing strategies ───────────────────────────

    def _type_into_smart_search(self, container, query: str) -> bool:
        """Try several interaction models until text appears.

        The smart search is a React component rendered as a plain
        ``<div>`` — no ``<input>`` is exposed.  Keyboard events
        must reach the component through the browser's native event
        pipeline, which rules out value-setting hacks.
        """

        strategies = [
            ("move + click, then individual ActionChains per char",
             self._ss_strat_individual_actions),
            ("click container element, then element.send_keys()",
             self._ss_strat_element_send_keys),
            ("click, then active-element send_keys()",
             self._ss_strat_active_element),
            ("JS focus + click, then individual ActionChains",
             self._ss_strat_js_focus_actions),
        ]

        for name, fn in strategies:
            logger.info(f"Smart search strategy: {name}")
            self._clear_smart_search(container)

            try:
                fn(container, query)
            except Exception as exc:
                logger.debug(f"  Strategy raised: {exc}")
                continue

            if self._verify_search_text(container, query):
                logger.info(f"  ✓ text verified")
                return True
            logger.info(f"  ✗ text not detected")

        logger.error("All smart search strategies failed")
        return False

    # ---- strategy implementations ----

    def _ss_strat_individual_actions(self, container, query):
        """Click with ActionChains, then send one key per perform()."""
        ActionChains(self.driver) \
            .move_to_element(container) \
            .click() \
            .perform()
        time.sleep(0.8)

        for ch in query:
            ActionChains(self.driver).send_keys(ch).perform()
            time.sleep(random.uniform(0.06, 0.12))

    def _ss_strat_element_send_keys(self, container, query):
        """Native click on the container, then container.send_keys()."""
        ActionChains(self.driver) \
            .move_to_element(container) \
            .click() \
            .perform()
        time.sleep(0.8)

        for ch in query:
            container.send_keys(ch)
            time.sleep(random.uniform(0.06, 0.12))

    def _ss_strat_active_element(self, container, query):
        """Click the container, read whatever the browser focused,
        and type into that."""
        ActionChains(self.driver) \
            .move_to_element(container) \
            .click() \
            .perform()
        time.sleep(0.8)

        active = self.driver.switch_to.active_element
        logger.debug(f"  active element tag: {active.tag_name}")
        for ch in query:
            active.send_keys(ch)
            time.sleep(random.uniform(0.06, 0.12))

    def _ss_strat_js_focus_actions(self, container, query):
        """Use JavaScript to focus + click the element, then type
        via ActionChains so keystrokes are native (trusted)."""
        self.driver.execute_script("""
            var el = arguments[0];
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true}));
            el.dispatchEvent(new MouseEvent('click',     {bubbles:true}));
            el.focus();
        """, container)
        time.sleep(0.8)

        for ch in query:
            ActionChains(self.driver).send_keys(ch).perform()
            time.sleep(random.uniform(0.06, 0.12))

    # ---- helpers ----

    def _clear_smart_search(self, container):
        """Select-all + delete inside the smart search."""
        try:
            ActionChains(self.driver) \
                .move_to_element(container) \
                .click() \
                .pause(0.3) \
                .key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL) \
                .send_keys(Keys.BACKSPACE) \
                .perform()
            time.sleep(0.3)
        except Exception:
            pass

    def _verify_search_text(self, container, query: str) -> bool:
        """Return True if the query appears in the search component."""
        time.sleep(0.6)
        prefix = query[:8].lower()

        # Check the container's own text content
        for prop in ("textContent", "innerText"):
            text = (
                self.driver.execute_script(
                    f"return arguments[0].{prop} || '';", container
                ) or ""
            )
            if prefix in text.lower():
                return True

        # Check innerHTML for text buried in child spans
        inner = (
            self.driver.execute_script(
                "return arguments[0].innerHTML || '';", container
            ) or ""
        )
        if prefix in inner.lower():
            return True

        # An autocomplete/suggestion dropdown appearing also counts
        for sel in (
            "[class*='suggestion']",
            "[class*='autocomplete']",
            ".smart-search-dropdown",
        ):
            try:
                hits = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if any(h.is_displayed() for h in hits):
                    return True
            except Exception:
                continue

        return False

    def _submit_smart_search(self, container):
        """Click the first visible autocomplete suggestion, or fall
        back to the search button / Enter."""
        # Try autocomplete suggestions first
        for sel in (
            "[class*='suggestion'] li",
            "[class*='autocomplete'] li",
            ".smart-search-dropdown li",
        ):
            try:
                items = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for item in items:
                    if item.is_displayed() and item.text.strip():
                        self._safe_click(item)
                        logger.info(
                            f"Selected autocomplete: '{item.text.strip()[:40]}'"
                        )
                        return
            except Exception:
                continue

        # Fall back to the search button
        submit = self._find_first(self.SELECTORS["search_submit"])
        if submit:
            self._safe_click(submit)
            logger.info("Clicked search submit button")
        else:
            ActionChains(self.driver).send_keys(Keys.RETURN).perform()
            logger.info("Pressed Enter to submit search")

    # ════════════════════════════════════════════════════════════════
    #  Advanced-filter panel
    # ════════════════════════════════════════════════════════════════

    def _apply_filters(self):
        toggle = self._find_first(self.SELECTORS["filters_button"])
        if not toggle:
            logger.warning("Advanced-filters button not found — skipping")
            return
        self._safe_click(toggle)
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS["filters_panel"], timeout=5
            )
        except TimeoutException:
            logger.warning("Advanced-filters panel did not appear")
            return
        time.sleep(0.8)

        if self.min_price:
            logger.info(f"Setting min price: ${self.min_price}")
            self._fill_filter_input(
                self.SELECTORS["filter_min_rent"], str(self.min_price)
            )
        if self.max_price:
            logger.info(f"Setting max price: ${self.max_price}")
            self._fill_filter_input(
                self.SELECTORS["filter_max_rent"], str(self.max_price)
            )
        time.sleep(0.5)

        self._set_beds_filter()
        time.sleep(0.5)
        self._set_baths_filter()
        time.sleep(0.5)
        self._set_sqft_filter()
        time.sleep(0.5)

        apply_btn = self._find_first(self.SELECTORS["apply_button"])
        if apply_btn:
            self._safe_click(apply_btn)
            logger.info("Clicked apply-filters button")
        else:
            logger.warning("Apply button not found; closing via toggle")
            toggle = self._find_first(self.SELECTORS["filters_button"])
            if toggle:
                self._safe_click(toggle)

        self._wait_for_results()

    def _fill_filter_input(self, selector: str, value: str):
        el = self._find_first(selector)
        if not el:
            logger.warning(f"Filter input not found: {selector[:60]}…")
            return
        try:
            el.click()
            time.sleep(0.2)
            el.send_keys(Keys.CONTROL, "a")
            time.sleep(0.1)
            el.send_keys(value)
            el.send_keys(Keys.TAB)
            time.sleep(0.3)
        except Exception as exc:
            logger.warning(f"Could not fill filter input: {exc}")

    def _set_beds_filter(self):
        if self.min_beds is None:
            return
        is_exact = self.max_beds is not None and self.min_beds == self.max_beds

        if self.min_beds == 0:
            if is_exact or self.max_beds == 0:
                self._click_chip("beds_chip", self._BED_CHILD[0])
                logger.info("Beds filter → Studio")
            return

        child_n = self._BED_CHILD.get(
            min(self.min_beds, 4), self._BED_CHILD[4]
        )
        self._click_chip("beds_chip", child_n)
        time.sleep(0.4)
        if is_exact:
            self._click_chip("beds_chip", child_n)
            time.sleep(0.4)
            logger.info(f"Beds filter → {self.min_beds} (exact)")
        else:
            logger.info(f"Beds filter → {self.min_beds}+")

    def _set_baths_filter(self):
        if self.min_baths is None:
            return
        bath_int = int(self.min_baths)
        if bath_int < 1:
            return
        is_exact = (
            self.max_baths is not None and int(self.max_baths) == bath_int
        )
        child_n = self._BATH_CHILD.get(
            min(bath_int, 3), self._BATH_CHILD[3]
        )
        self._click_chip("baths_chip", child_n)
        time.sleep(0.4)
        if is_exact:
            self._click_chip("baths_chip", child_n)
            time.sleep(0.4)
            logger.info(f"Baths filter → {bath_int} (exact)")
        else:
            logger.info(f"Baths filter → {bath_int}+")

    def _set_sqft_filter(self):
        if self.min_sqft:
            self._set_sqft_dropdown("min", self.min_sqft)
        if self.max_sqft:
            self._set_sqft_dropdown("max", self.max_sqft)

    def _set_sqft_dropdown(self, which: str, target: int):
        closest = min(_SQFT_VALUES, key=lambda v: abs(v - target))
        item_n = _SQFT_INDEX[closest]

        dd = self._find_first(self.SELECTORS[f"sqft_{which}_dropdown"])
        if not dd:
            logger.warning(f"Sqft {which} dropdown not found")
            return
        self._safe_click(dd)
        time.sleep(0.6)

        text_sel = self.SELECTORS[f"sqft_{which}_item_text"].format(n=item_n)
        text_el = self._find_first(text_sel)
        if text_el:
            displayed = re.sub(r"[^\d]", "", text_el.text)
            if displayed and int(displayed) != closest:
                logger.warning(
                    f"Sqft {which} mismatch: expected {closest}, "
                    f"got {displayed} — aborting"
                )
                self._safe_click(dd)
                return

        item_sel = self.SELECTORS[f"sqft_{which}_item"].format(n=item_n)
        item = self._find_first(item_sel)
        if item:
            self._safe_click(item)
            logger.info(f"Sqft {which} → {closest}")
        else:
            logger.warning(f"Sqft {which} menu item {item_n} not found")
        time.sleep(0.4)

    def _click_chip(self, selector_key: str, n: int):
        sel = self.SELECTORS[selector_key].format(n=n)
        el = self._find_first(sel)
        if el:
            self._safe_click(el)
        else:
            logger.warning(f"Chip not found: {sel[:80]}…")

    def _wait_for_results(self):
        self.delay((2.0, 4.0))
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS["listing_card"], timeout=10
            )
        except TimeoutException:
            pass

    # ════════════════════════════════════════════════════════════════
    #  Extraction
    # ════════════════════════════════════════════════════════════════

    def _has_listings(self) -> bool:
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS["listing_card"], timeout=10
            )
            cards = self.find_elements_safe(
                By.CSS_SELECTOR, self.SELECTORS["listing_card"]
            )
            logger.info(f"apartments.com: {len(cards)} cards visible")
            return len(cards) > 0
        except TimeoutException:
            return False

    def get_listings_from_page(self) -> List[RentalListing]:
        listings: List[RentalListing] = []
        self._scroll_page()
        self.short_delay()

        soup = BeautifulSoup(self.get_page_source(), "lxml")
        cards = soup.select(self.SELECTORS["listing_card"])
        logger.info(f"apartments.com: parsing {len(cards)} cards")

        for i, card in enumerate(cards):
            try:
                listing = self._parse_card(card)
                if listing and listing.id not in self._seen_ids:
                    self._seen_ids.add(listing.id)
                    listings.append(listing)
                    if listing.address.city:
                        self._seen_cities.add(listing.address.city)
            except Exception as exc:
                logger.debug(f"Card {i} parse error: {exc}")

        logger.info(f"Extracted {len(listings)} unique listings")
        return listings

    def _parse_card(self, card: Tag) -> Optional[RentalListing]:
        link_el = None
        for sel in self.SELECTORS["card_link"].split(","):
            link_el = card.select_one(sel.strip())
            if link_el:
                break
        if not link_el:
            link_el = card.select_one("a[href]")
        if not link_el:
            return None
        href = link_el.get("href", "")
        url = href if href.startswith("http") else self.BASE_URL + href

        title_el = None
        for sel in self.SELECTORS["card_title"].split(","):
            title_el = card.select_one(sel.strip())
            if title_el:
                break
        title = title_el.get_text(strip=True) if title_el else ""

        addr_el = None
        for sel in self.SELECTORS["card_address"].split(","):
            addr_el = card.select_one(sel.strip())
            if addr_el:
                break
        addr_text = addr_el.get_text(" ", strip=True) if addr_el else title
        if not addr_text:
            return None

        price_el = None
        for sel in self.SELECTORS["card_price"].split(","):
            price_el = card.select_one(sel.strip())
            if price_el:
                break
        price_text = price_el.get_text(strip=True) if price_el else ""
        base_rent = self._parse_price_range(price_text)

        beds_el = None
        for sel in self.SELECTORS["card_beds"].split(","):
            beds_el = card.select_one(sel.strip())
            if beds_el:
                break
        beds = None
        if beds_el:
            beds_txt = beds_el.get_text(strip=True).lower()
            if "studio" in beds_txt:
                beds = 0
            else:
                m = re.search(r"(\d+)", beds_txt)
                if m:
                    beds = int(m.group(1))

        img_el = None
        for sel in self.SELECTORS["card_image"].split(","):
            img_el = card.select_one(sel.strip())
            if img_el:
                break
        img_url = ""
        if img_el:
            img_url = (
                img_el.get("data-src")
                or img_el.get("src")
                or img_el.get("data-lazy")
                or ""
            )

        listing_id_attr = (link_el or card).get("data-listingid", "")
        source_id = listing_id_attr or self._extract_source_id(url)
        city, province = self._parse_city_province(addr_text)

        address = Address(
            full_address=addr_text, city=city, province=province,
            country="Canada",
        )
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, addr_text)

        return RentalListing(
            id=lid, address=address,
            price=PriceInfo(base_rent=base_rent or 0, currency="CAD"),
            features=PropertyFeatures(
                bedrooms=beds, property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME, source_url=url,
                source_id=source_id,
                photo_urls=[img_url] if img_url else [],
            ),
            title=title or addr_text,
        )

    # ════════════════════════════════════════════════════════════════
    #  Pagination
    # ════════════════════════════════════════════════════════════════

    def go_to_next_page(self) -> bool:
        try:
            pag = self._find_first(self.SELECTORS["pagination_container"])
            if pag:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", pag
                )
                self.short_delay()

            btn = self._find_next_page_link()
            if not btn:
                return False

            cls_attr = (btn.get_attribute("class") or "").lower()
            if "disabled" in cls_attr:
                return False

            old_url = self.driver.current_url
            self._safe_click(btn)
            self.delay(self.PAGE_LOAD_DELAY)
            return self.driver.current_url != old_url or self._has_listings()
        except Exception as exc:
            logger.debug(f"next_page error: {exc}")
            return False

    def _find_next_page_link(self):
        for sel in self.SELECTORS["next_page"].split(","):
            sel = sel.strip()
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    text = (el.text or "").strip().lower()
                    if text not in ("prev", "previous", "\u2039", "\u00ab"):
                        return el
            except NoSuchElementException:
                continue

        try:
            links = self.driver.find_elements(
                By.CSS_SELECTOR, "#paging > ol > li > a"
            )
            for link in reversed(links):
                text = (link.text or "").strip().lower()
                if text in ("next", "\u203a", "\u00bb", ">"):
                    return link
                aria = (link.get_attribute("aria-label") or "").lower()
                if "next" in aria:
                    return link
        except Exception:
            pass
        return None

    # ════════════════════════════════════════════════════════════════
    #  Helpers
    # ════════════════════════════════════════════════════════════════

    def _find_first(self, multi_selector: str):
        for sel in multi_selector.split(","):
            sel = sel.strip()
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    return el
            except NoSuchElementException:
                continue
        return None

    def _safe_click(self, el):
        try:
            el.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", el)

    def _scroll_page(self):
        try:
            last = 0
            for _ in range(15):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(300,600)});"
                )
                time.sleep(random.uniform(0.2, 0.5))
                cur = self.driver.execute_script("return window.pageYOffset")
                if cur == last:
                    break
                last = cur
            self.driver.execute_script("window.scrollTo(0,0);")
            time.sleep(0.3)
        except Exception:
            pass

    @staticmethod
    def _parse_price_range(text: str) -> Optional[float]:
        if not text:
            return None
        prices = re.findall(r"\$?([\d,]+)", text.replace(" ", ""))
        if prices:
            return float(prices[0].replace(",", ""))
        return None

    @staticmethod
    def _extract_source_id(url: str) -> str:
        m = re.search(r"/(\d{5,})", url)
        if m:
            return m.group(1)
        parts = url.rstrip("/").split("/")
        return parts[-1][:20] if parts else str(abs(hash(url)))[:12]

    @staticmethod
    def _parse_city_province(addr_text: str):
        parts = [p.strip() for p in addr_text.split(",") if p.strip()]
        province = "QC"
        city = ""
        if len(parts) >= 2:
            last = parts[-1].strip().upper()
            if len(last) == 2:
                province = last
                city = parts[-2] if len(parts) >= 2 else ""
            elif any(prov in last for prov in ("QC", "ON", "BC", "AB")):
                m = re.search(r"\b([A-Z]{2})\b", last)
                if m:
                    province = m.group(1)
                city = parts[-2] if len(parts) >= 3 else parts[0]
            else:
                city = parts[-1]
        elif parts:
            city = parts[0]
        return city, province

    @staticmethod
    def _apply_amenity(listing: RentalListing, txt: str):
        if "dishwasher" in txt:
            listing.amenities.dishwasher = True
        if "gym" in txt or "fitness" in txt:
            listing.amenities.gym = True
        if "pool" in txt and "carpool" not in txt:
            listing.amenities.pool = True
        if "elevator" in txt:
            listing.amenities.elevator = True
        if "concierge" in txt or "doorman" in txt:
            listing.amenities.concierge = True
        if "rooftop" in txt:
            listing.amenities.rooftop = True
        if "balcony" in txt or "patio" in txt:
            listing.features.balcony = True
        if "a/c" in txt or "air condition" in txt:
            listing.features.air_conditioning = True
        if "washer" in txt or "laundry" in txt:
            if "in-unit" in txt or "in unit" in txt or "suite" in txt:
                listing.features.laundry = LaundryType.IN_UNIT
            elif "hookup" in txt:
                listing.features.laundry = LaundryType.HOOKUPS
            elif "shared" in txt or "common" in txt:
                listing.features.laundry = LaundryType.IN_BUILDING
        if "parking" in txt or "garage" in txt:
            listing.features.parking_type = ParkingType.INDOOR