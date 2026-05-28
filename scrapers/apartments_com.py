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
import copy

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
    LaundryType, RentalListing, RentValue,
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

_FP_VACANCY_CONTAINER = "#pricingView > div.tab-section.active"
_FP_VACANCY_ROW = f"{_FP_VACANCY_CONTAINER} > div"
_FP_DETAILS_BTN_TPL = (
    f"{_FP_VACANCY_CONTAINER} > div:nth-child({{n}}) > div > div "
    "> div.column2 > div > div.actionLinksContainer "
    "> button.actionLinks.js-viewModelDetails-modal"
)
_FP_MODAL_ROOT = "#rentalDetailModalContentContainer"
_FP_CLOSE_BTN = "#closeRentalDetailButton"
_FP_RENT = (
    f"{_FP_MODAL_ROOT} > div > div.left-unit-detail-container.amenities "
    "> div.one-col > div > div.specs-header.no-wrap.pricing"
)
_FP_SPECS_LI_TPL = (
    f"{_FP_MODAL_ROOT} > div > div.left-unit-detail-container.amenities "
    "> div.one-col > div > div:nth-child(3) > ul > li:nth-child({n})"
)
_FP_ACTIVE_IMG = "#activeMedia"
_FP_NEXT_IMG = (
    "#rentalDetailCarouselSection > div.navigationControl "
    "> button.rightNav.js-rentalModalMediaRightNav"
)
_FP_AMENITY_UL = (
    f"{_FP_MODAL_ROOT} > div > div.left-unit-detail-container.amenities "
    "> div.amenities > ul"
)

class ApartmentsComScraper(BaseScraper):
    SITE_NAME = "apartments.com"
    BASE_URL = "https://www.apartments.com"

    # ────────────────────────────────────────────────────────────
    #  Selectors
    # ────────────────────────────────────────────────────────────

    SELECTORS = {
        # ── Homepage search (smart search — NO child <input>) ────
        "search_input": ".smart-search-input[contenteditable]",
        
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

    def enrich_listings(self, stubs):
        """Expand each property stub into one listing per floor-plan vacancy."""
        if not stubs:
            return stubs
        out = []
        for i, stub in enumerate(stubs):
            logger.info(f"  [{i+1}/{len(stubs)}] {stub.metadata.source_url}")
            try:
                plans = self._extract_property_detail(stub)
                if plans:
                    out.extend(plans)
                    logger.info(f"    → {len(plans)} floor plan(s)")
                else:
                    out.append(stub)
                    logger.info("    → 0 floor plans, keeping stub")
            except Exception as exc:
                logger.warning(f"    Detail error: {exc}")
                out.append(stub)
            if i < len(stubs) - 1:
                self.delay((2.5, 5.0))
        logger.info(f"Total after enrichment: {len(out)}")
        return out
    
    def _extract_property_detail(self, stub):
        self.navigate(stub.metadata.source_url)
        self._dismiss_popups()
        self.short_delay()
        self._scroll_page()

        try:
            self.wait_for_element(By.CSS_SELECTOR, _FP_VACANCY_CONTAINER, timeout=10)
        except TimeoutException:
            return []

        rows = self.find_elements_safe(By.CSS_SELECTOR, _FP_VACANCY_ROW)
        n_rows = len(rows)
        logger.info(f"    {n_rows} vacancy row(s)")

        listings = []
        for idx in range(n_rows):
            try:
                plan = self._extract_floor_plan(stub, idx)
                if plan:
                    listings.append(plan)
            except Exception as exc:
                logger.debug(f"    Vacancy {idx} error: {exc}")
            finally:
                self._close_floor_plan_modal()
            time.sleep(0.6)
        return listings

    def _extract_floor_plan(self, stub, idx):
        btn_sel = _FP_DETAILS_BTN_TPL.format(n=idx + 1)
        btn = self.find_element_safe(By.CSS_SELECTOR, btn_sel)
        if not btn:
            return None
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", btn
        )
        time.sleep(0.4)
        self._safe_click(btn)

        try:
            self.wait_for_element(By.CSS_SELECTOR, _FP_RENT, timeout=8)
        except TimeoutException:
            return None
        time.sleep(0.5)

        soup = BeautifulSoup(self.get_page_source(), "lxml")
        rent  = self._parse_rent_range(self._text(soup, _FP_RENT))
        beds  = self._scan_beds(self._text(soup, _FP_SPECS_LI_TPL.format(n=1)))
        baths = self._scan_baths(self._text(soup, _FP_SPECS_LI_TPL.format(n=2)))
        sqft  = self._parse_sqft_range(self._text(soup, _FP_SPECS_LI_TPL.format(n=3)))
        amenity_map = self._parse_modal_amenities(soup)
        images = self._collect_modal_images()

        src_id = f"{stub.metadata.source_id}_fp{idx}"
        lid = RentalListing.generate_id(
            self.SITE_NAME, src_id, stub.metadata.source_url
        )
        listing = RentalListing(
            id=lid,
            address=copy.deepcopy(stub.address),
            price=PriceInfo(
                base_rent=rent or RentValue(amount=0),
                currency="CAD",
            ),
            features=PropertyFeatures(
                bedrooms=beds, bathrooms=baths, square_feet=sqft,
                property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=stub.metadata.source_url,
                source_id=src_id,
                photo_urls=images,
            ),
            title=(
                f"{stub.address.full_address} – "
                f"{'Studio' if beds == 0 else f'{beds} BR'}"
                if beds is not None else stub.address.full_address
            ),
        )
        self._apply_amenity_map(listing, amenity_map)
        return listing

    def _close_floor_plan_modal(self):
        btn = self.find_element_safe(By.CSS_SELECTOR, _FP_CLOSE_BTN)
        if btn and btn.is_displayed():
            try:
                self._safe_click(btn)
                time.sleep(0.4)
                return
            except Exception:
                pass
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass

    def _collect_modal_images(self, max_images: int = 25) -> List[str]:
        images: List[str] = []
        seen = set()
        first_src = None
        for step in range(max_images):
            img = self.find_element_safe(By.CSS_SELECTOR, _FP_ACTIVE_IMG)
            src = (
                (img.get_attribute("src") or img.get_attribute("data-src") or "")
                if img else ""
            )
            if src and src not in seen:
                images.append(src)
                seen.add(src)
                if first_src is None:
                    first_src = src
            elif src and src == first_src and step > 0:
                break   # carousel wrapped

            nxt = self.find_element_safe(By.CSS_SELECTOR, _FP_NEXT_IMG)
            if not nxt or not nxt.is_displayed():
                break
            try:
                self._safe_click(nxt)
                time.sleep(0.35)
            except Exception:
                break
        return images

    def _parse_modal_amenities(self, soup) -> Dict[str, List[str]]:
        """Return ``{category: [items]}`` from the modal's amenity list."""
        out: Dict[str, List[str]] = {}
        ul = soup.select_one(_FP_AMENITY_UL)
        if not ul:
            return out
        for li in ul.select(":scope > li"):
            cat_el = li.select_one(":scope > span")
            items_ul = li.select_one(":scope > ul")
            category = cat_el.get_text(strip=True) if cat_el else "Other"
            items = (
                [it.get_text(strip=True)
                for it in items_ul.select(":scope > li")
                if it.get_text(strip=True)]
                if items_ul else []
            )
            out[category] = items
        return out

    def _apply_amenity_map(self, listing, amenity_map):
        flat: List[str] = []
        for cat, items in amenity_map.items():
            for item in items:
                flat.append(f"{cat}: {item}")
                self._apply_amenity(listing, item.lower())
        if flat:
            listing.amenities.other_amenities = flat

    # ── Parsers ─────────────────────────────────────────────────────────

    @staticmethod
    def _text(soup, sel: str) -> str:
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""

    @staticmethod
    def _parse_rent_range(text: str) -> Optional[RentValue]:
        """Handle '$2,100', '$2,100 – $2,800', 'Call for pricing', etc."""
        if not text:
            return None
        t = (text.replace(",", "")
                .replace("\u2013", "-")
                .replace("\u2014", "-"))
        nums = [float(n) for n in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t)]
        nums = [n for n in nums if 100 <= n <= 50000]
        if not nums:
            return None
        if len(nums) >= 2 and max(nums) != min(nums):
            lo, hi = min(nums), max(nums)
            return RentValue(amount=lo, min_amount=lo, max_amount=hi)
        return RentValue(amount=nums[0])

    @staticmethod
    def _parse_sqft_range(text: str) -> Optional[int]:
        if not text:
            return None
        nums = [
            int(n.replace(",", ""))
            for n in re.findall(r"(\d[\d,]*)", text)
        ]
        nums = [n for n in nums if n > 50]
        return min(nums) if nums else None

    # ════════════════════════════════════════════════════════════════
    #  search_city
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL)
            self._dismiss_popups()
            self.short_delay()

            query = f"{city_name}, QC, Canada"

            try:
                container = self.wait_for_element(
                    By.CSS_SELECTOR,
                    self.SELECTORS["search_input"],
                    timeout=10,
                )
            except TimeoutException:
                logger.error("apartments.com: search input not found")
                return False

            ActionChains(self.driver) \
                .move_to_element(container) \
                .click() \
                .perform()
            time.sleep(0.8)

            for ch in query:
                ActionChains(self.driver).send_keys(ch).perform()
                time.sleep(random.uniform(0.06, 0.12))

            time.sleep(1.5)
            self._submit_smart_search()

            self.delay(self.PAGE_LOAD_DELAY)
            self._dismiss_popups()
            self._apply_filters()
            self.medium_delay()

            return self._has_listings()
        except Exception as exc:
            logger.error(f"apartments.com search_city failed: {exc}")
            import traceback; traceback.print_exc()
            return False

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

    def _submit_smart_search(self):
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
                if listing:
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

        # Selector-based extraction (may miss on UNVERIFIED markup).
        price_el = self._select_first(card, self.SELECTORS["card_price"])
        beds_el  = self._select_first(card, self.SELECTORS["card_beds"])
        baths_el = self._select_first(card, ".bath-range, .property-baths")

        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        beds_text  = beds_el.get_text(" ", strip=True)  if beds_el  else ""
        baths_text = baths_el.get_text(" ", strip=True) if baths_el else ""

        # Text-level fallback over the whole card — survives selector drift.
        full_text = card.get_text(" ", strip=True)
        base_rent = (
            self._parse_price_range(price_text)
            or self._scan_price(full_text)
        )
        beds = self._scan_beds(beds_text) or self._scan_beds(full_text)
        baths = self._scan_baths(baths_text) or self._scan_baths(full_text)

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
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)

        return RentalListing(
            id=lid, 
            address=address,
            price=PriceInfo(
                base_rent=RentValue(amount=base_rent or 0),
                currency="CAD",
            ),
            features=PropertyFeatures(
                bedrooms=beds, 
                bathrooms=baths,
                property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME, source_url=url,
                source_id=source_id,
                photo_urls=[img_url] if img_url else [],
            ),
            title=title or addr_text,
        )
    
    @staticmethod
    def _select_first(card: Tag, csv: str) -> Optional[Tag]:
        for sel in csv.split(","):
            el = card.select_one(sel.strip())
            if el:
                return el
        return None

    @staticmethod
    def _scan_price(text: str) -> Optional[float]:
        # Avoid grabbing deposit/fee figures: anchor on "$NNNN" followed by
        # optional /mo or whitespace, take the smallest sensible match.
        candidates = re.findall(r"\$\s*([\d,]{3,})", text)
        values = [float(c.replace(",", "")) for c in candidates if c]
        values = [v for v in values if 300 <= v <= 15000]
        return min(values) if values else None

    @staticmethod
    def _scan_beds(text: str) -> Optional[int]:
        if not text:
            return None
        t = text.lower()
        if "studio" in t or "bachelor" in t:
            return 0
        m = re.search(r"(\d+)\s*(?:bd|bed|br)\b", t)
        return int(m.group(1)) if m else None

    @staticmethod
    def _scan_baths(text: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ba|bath)\b", text.lower())
        return float(m.group(1)) if m else None

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