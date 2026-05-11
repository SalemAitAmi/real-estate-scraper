"""
Apartments.com scraper — Selenium + undetected_chromedriver.

apartments.com is more tightly bounded than realtor.ca / rentals.ca:
results include only a small fringe of listings from adjacent
jurisdictions.  The ``skip_covered_locations`` toggle is still
honoured but has less impact here.

NOTE: apartments.com is US-centric; Canadian coverage is thinner.
Selectors below are best-effort baselines — inspect live and adjust.
"""

import logging
import random
import re
import time
from typing import Dict, List, Optional, Any
from datetime import datetime

from bs4 import BeautifulSoup, Tag
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


class ApartmentsComScraper(BaseScraper):
    """
    Scraper for https://www.apartments.com

    Apartments.com often displays *buildings* (with price ranges) rather
    than individual units.  The scraper records the minimum advertised
    price and links to the building page for further exploration.
    """

    SITE_NAME = "apartments.com"
    BASE_URL = "https://www.apartments.com"

    SELECTORS = {
        # Search
        "search_input": (
            "#searchBarLookup, "
            "input#quickSearchLookup, "
            "input.search-bar-input"
        ),
        "search_submit": (
            "#searchBarSubmit, "
            "button.go-btn, "
            "button[type='submit']"
        ),

        # Filters (top bar or sidebar)
        "filter_min_rent": (
            "#TextMinRent, "
            "input[name='MinRent'], "
            "input.min-rent"
        ),
        "filter_max_rent": (
            "#TextMaxRent, "
            "input[name='MaxRent'], "
            "input.max-rent"
        ),
        "filter_beds": (
            "#TextBeds, "
            "button[data-beds='{n}'], "
            ".bed-options button[value='{n}']"
        ),
        "filter_baths": (
            "#TextBaths, "
            "button[data-baths='{n}'], "
            ".bath-options button[value='{n}']"
        ),
        "apply_btn": (
            "button.btn-apply, "
            "button[data-testid='apply-filters']"
        ),
        "sort_dropdown": (
            "select#TextSortOptions, "
            "select.sort-select"
        ),

        # Results
        "results_container": (
            "#placardContainer, "
            ".search-results, "
            "#searchResults"
        ),
        "listing_card": (
            "li.mortar-wrapper article, "
            "article.placard, "
            ".property-card"
        ),

        # Card internals
        "card_link": (
            "a.property-link, "
            "a[data-listingid], "
            "a[href*='/apartments/']"
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
        "card_beds": (
            ".bed-range, "
            ".property-beds"
        ),
        "card_image": (
            "img.lazyload, "
            "img[data-src], "
            ".property-image img"
        ),

        # Pagination
        "next_page": (
            "a.next, "
            "a[data-page='next'], "
            ".paging a.next"
        ),
        "pagination_container": (
            ".paging, "
            ".pagination, "
            "nav[aria-label='Pagination']"
        ),

        # Detail page
        "detail": {
            "description": (
                ".descriptionSection p, "
                "#TextDescription"
            ),
            "amenity_items": (
                ".amenityCard li, "
                ".amenitiesSection li"
            ),
            "fee_items": (
                ".feesSection li, "
                ".pricingSection li"
            ),
            "pet_section": (
                ".petSection, "
                "[data-testid='pet-policy']"
            ),
            "contact_name": ".contactName, .managerName",
            "contact_phone": "a[href^='tel:']",
        },
    }

    # ── Constructor ──────────────────────────────────────────────

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ── Post-scrape hook ─────────────────────────────────────────

    def _post_scrape(self, listings):
        if self.fetch_details and listings:
            logger.info(
                f"Fetching details for {len(listings)} apartments.com listings"
            )
            return self._enrich_all(listings)
        return listings

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

        # Description
        desc_el = soup.select_one(sel["description"])
        if desc_el:
            listing.description = desc_el.get_text(" ", strip=True)

        # Amenities
        amenity_texts = []
        for li in soup.select(sel["amenity_items"]):
            txt = li.get_text(strip=True)
            amenity_texts.append(txt)
            self._apply_amenity(listing, txt.lower())
        if amenity_texts:
            listing.amenities.other_amenities = amenity_texts

        # Fees
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

        # Pet policy
        pet_el = soup.select_one(sel["pet_section"])
        if pet_el:
            pt = pet_el.get_text(strip=True).lower()
            listing.features.pets_allowed = "allowed" in pt or "friendly" in pt
            listing.features.cats_allowed = "cat" in pt and "no cat" not in pt
            listing.features.dogs_allowed = "dog" in pt and "no dog" not in pt

        # Contact
        cn = soup.select_one(sel["contact_name"])
        if cn:
            listing.metadata.contact_name = cn.get_text(strip=True)
        cp = soup.select_one(sel["contact_phone"])
        if cp:
            listing.metadata.contact_phone = cp.get_text(strip=True)

        return listing

    # ── search_city ──────────────────────────────────────────────

    def search_city(self, city_name: str) -> bool:
        try:
            self.navigate(self.BASE_URL)
            self._dismiss_popups()
            self.short_delay()

            search = self._find_first(self.SELECTORS["search_input"])
            if not search:
                logger.error("apartments.com: search input not found")
                return False
            search.clear()
            self.type_slowly(search, f"{city_name}, QC, Canada")
            time.sleep(1.5)

            submit = self._find_first(self.SELECTORS["search_submit"])
            if submit:
                self._safe_click(submit)
            else:
                search.send_keys(Keys.RETURN)

            self.delay(self.PAGE_LOAD_DELAY)
            self._dismiss_popups()

            self._apply_filters()
            self.medium_delay()

            return self._has_listings()
        except Exception as exc:
            logger.error(f"apartments.com search_city failed: {exc}")
            return False

    # ── Filters ──────────────────────────────────────────────────

    def _apply_filters(self):
        # Price
        if self.min_price:
            self._set_input_value(self.SELECTORS["filter_min_rent"], str(self.min_price))
        if self.max_price:
            self._set_input_value(self.SELECTORS["filter_max_rent"], str(self.max_price))

        # Beds
        if self.min_beds is not None:
            sel = self.SELECTORS["filter_beds"].replace("{n}", str(self.min_beds))
            btn = self._find_first(sel)
            if btn:
                self._safe_click(btn)

        # Baths
        if self.min_baths is not None:
            sel = self.SELECTORS["filter_baths"].replace("{n}", str(int(self.min_baths)))
            btn = self._find_first(sel)
            if btn:
                self._safe_click(btn)

        # Apply
        apply_btn = self._find_first(self.SELECTORS["apply_btn"])
        if apply_btn:
            self._safe_click(apply_btn)
            self._wait_for_results()

        # Sort by newest
        sort_el = self._find_first(self.SELECTORS["sort_dropdown"])
        if sort_el:
            try:
                from selenium.webdriver.support.ui import Select
                select = Select(sort_el)
                for opt in select.options:
                    if "new" in opt.text.lower() or "date" in opt.text.lower():
                        select.select_by_visible_text(opt.text)
                        self._wait_for_results()
                        return
            except Exception:
                pass

    def _wait_for_results(self):
        self.delay((2.0, 4.0))
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS["results_container"], timeout=10
            )
        except TimeoutException:
            pass

    # ── Extraction ───────────────────────────────────────────────

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
        cards: List[Tag] = []
        for sel in self.SELECTORS["listing_card"].split(","):
            cards = soup.select(sel.strip())
            if cards:
                break

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
        # Link
        link_el = card.select_one(self.SELECTORS["card_link"])
        if not link_el:
            link_el = card.select_one("a[href]")
        if not link_el:
            return None
        href = link_el.get("href", "")
        url = href if href.startswith("http") else self.BASE_URL + href

        # Title (often the building/property name)
        title_el = card.select_one(self.SELECTORS["card_title"])
        title = title_el.get_text(strip=True) if title_el else ""

        # Address
        addr_el = card.select_one(self.SELECTORS["card_address"])
        addr_text = addr_el.get_text(" ", strip=True) if addr_el else title
        if not addr_text:
            return None

        # Price (may be a range like "$1,200 – $1,800")
        price_el = card.select_one(self.SELECTORS["card_price"])
        price_text = price_el.get_text(strip=True) if price_el else ""
        base_rent = self._parse_price_range(price_text)

        # Beds
        beds_el = card.select_one(self.SELECTORS["card_beds"])
        beds = None
        if beds_el:
            beds_txt = beds_el.get_text(strip=True).lower()
            if "studio" in beds_txt:
                beds = 0
            else:
                m = re.search(r"(\d+)", beds_txt)
                if m:
                    beds = int(m.group(1))

        # Image
        img_el = card.select_one(self.SELECTORS["card_image"])
        img_url = ""
        if img_el:
            img_url = (
                img_el.get("data-src")
                or img_el.get("src")
                or img_el.get("data-lazy")
                or ""
            )

        # Source ID
        listing_id_attr = (link_el or card).get("data-listingid", "")
        source_id = listing_id_attr or self._extract_source_id(url)

        # Address components
        city, province = self._parse_city_province(addr_text)

        address = Address(
            full_address=addr_text,
            city=city,
            province=province,
            country="Canada",
        )

        lid = RentalListing.generate_id(self.SITE_NAME, source_id, addr_text)

        return RentalListing(
            id=lid,
            address=address,
            price=PriceInfo(base_rent=base_rent or 0, currency="CAD"),
            features=PropertyFeatures(
                bedrooms=beds,
                property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=url,
                source_id=source_id,
                photo_urls=[img_url] if img_url else [],
            ),
            title=title or addr_text,
        )

    # ── Pagination ───────────────────────────────────────────────

    def go_to_next_page(self) -> bool:
        try:
            pag = self._find_first(self.SELECTORS["pagination_container"])
            if pag:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", pag
                )
                self.short_delay()

            btn = self._find_first(self.SELECTORS["next_page"])
            if not btn or not btn.is_displayed():
                return False
            cls = (btn.get_attribute("class") or "").lower()
            if "disabled" in cls:
                return False

            old_url = self.driver.current_url
            self._safe_click(btn)
            self.delay(self.PAGE_LOAD_DELAY)
            return self.driver.current_url != old_url or self._has_listings()
        except Exception as exc:
            logger.debug(f"next_page error: {exc}")
            return False

    # ── Helpers ───────────────────────────────────────────────────

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

    def _set_input_value(self, multi_sel: str, value: str):
        el = self._find_first(multi_sel)
        if not el:
            return
        try:
            el.clear()
            self.type_slowly(el, value)
            el.send_keys(Keys.TAB)
            time.sleep(0.5)
        except Exception:
            pass

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
        """Return the *minimum* price from text like '$1,200 – $1,800'."""
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
                # "Montreal, QC H3A 1B1" → extract province code
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