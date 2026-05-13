"""
Realtor.ca scraper — Selenium + undetected_chromedriver.
"""

import logging
import random
import re
import time
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timedelta
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, HeatingType, ParkingType,
    LaundryType, RentalListing,
)
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class RealtorCaScraper(BaseScraper):
    SITE_NAME = "realtor.ca"
    BASE_URL = "https://www.realtor.ca"

    SELECTORS = {
        'search_input': '#homeSearchTxt',
        'list_view_button': '#mapViewToggle > div > div > a:nth-child(2)',
        'selected_toggle': '#mapViewToggle > div > div > a.toggleOption.selected',
        'transaction_type': '#ddlTransactionTypeTopRes-container',
        'min_price': '#ddlMinRentTop-container',
        'max_price': '#ddlMaxRentTop-container',
        'beds': '#ddlBedsTop-container',
        'baths': '#ddlBathsTop-container',
        'sort_by': '#ddlListResultsSort-container',
        'dropdown_search_input': (
            'span.select2-search.select2-search--dropdown '
            '> input.select2-search__field'
        ),
        'dropdown_options': 'li.select2-results__option',
        'next_page': (
            '#ListViewPagination_Bottom > div '
            '> a.lnkNextResultsPage.paginationLink'
            '.paginationLinkForward.btn.small'
        ),
        'pagination_container': '#ListViewPagination_Bottom',
        'listing_container_list': '#listInnerCon',
        'card_wrapper': '#listInnerCon > div.cardCon',
        'card_link': ':scope > a',
        'card_price': '.listingCardPrice',
        'card_address': '.listingCardAddress',
        'card_icon_strip': '.listingCardIconStrip',
        'card_icon_num': '.listingCardIconNum',
        'card_image': '.listingCardImageCon img',
        'detail': {
            'price_change': (
                '#listingDetailsTopInnerCon > div.leftTableCell '
                '> div.PriceChangeOnRealtorCon.tag.priceChangeOnRealtorTag'
            ),
            'property_type': (
                '#propertyDetailsSectionContentSubCon_PropertyType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'building_type': (
                '#propertyDetailsSectionContentSubCon_BuildingType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'storeys': (
                '#propertyDetailsSectionContentSubCon_Stories '
                '> div.propertyDetailsSectionContentValue'
            ),
            'neighbourhood': (
                '#propertyDetailsSectionContentSubCon_NeighborhoodName '
                '> div.propertyDetailsSectionContentValue'
            ),
            'year_built': (
                '#propertyDetailsSectionContentSubCon_BuiltIn '
                '> div.propertyDetailsSectionContentValue'
            ),
            'parking_type_summary': (
                '#propertyDetailsSectionContentSubCon_ParkingType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'time_on_realtor': (
                '#propertyDetailsSectionContentSubCon_TimeOnRealtor '
                '> div.propertyDetailsSectionContentValue'
            ),
            'features': (
                '#propertyDetailsSectionVal_Features '
                '> div.propertyDetailsSectionContentValue'
            ),
            'style': (
                '#propertyDetailsSectionVal_Style '
                '> div.propertyDetailsSectionContentValue'
            ),
            'cooling': (
                '#propertyDetailsSectionVal_Cooling '
                '> div.propertyDetailsSectionContentValue'
            ),
            'heating_type': (
                '#propertyDetailsSectionVal_HeatingType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'sewer': (
                '#propertyDetailsSectionVal_UtilitySewer '
                '> div.propertyDetailsSectionContentValue'
            ),
            'water': (
                '#propertyDetailsSectionVal_UtilityWater '
                '> div.propertyDetailsSectionContentValue'
            ),
            'pool_type': (
                '#propertyDetailsSectionVal_PoolType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'amenities_nearby': (
                '#propertyDetailsSectionVal_AmenitiesNearby '
                '> div.propertyDetailsSectionContentValue'
            ),
            'parking_type': (
                '#propertyDetailsSectionVal_ParkingType '
                '> div.propertyDetailsSectionContentValue'
            ),
            'total_parking': (
                '#propertyDetailsSectionVal_TotalParkingSpaces '
                '> div.propertyDetailsSectionContentValue'
            ),
            'description': '#listingDescriptionCon',
        },
    }

    TRANSACTION_TYPES = {'rent': 'For rent', 'buy': 'For sale'}
    SORT_OPTIONS = {'newest': 'Newest'}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    # ── Detail enrichment (Model A) ──────────────────────────────

    def enrich_listings(self, listings):
        if not listings:
            return listings
        return self._enrich_listings_with_details(listings)

    def _enrich_listings_with_details(self, listings):
        enriched = []
        total = len(listings)
        for i, listing in enumerate(listings):
            logger.info(
                f"Fetching details [{i+1}/{total}]: "
                f"{listing.address.full_address[:50]}…"
            )
            try:
                enriched.append(self._fetch_listing_details(listing))
            except Exception as exc:
                logger.error(f"Detail error for {listing.id}: {exc}")
                enriched.append(listing)
            if i < total - 1:
                self.delay((3.0, 6.0))
        return enriched

    def _fetch_listing_details(self, listing):
        url = listing.metadata.source_url
        if not url:
            return listing
        self.navigate(url)
        self._dismiss_popups()
        self.short_delay()
        self._scroll_detail_page()
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        details = self._parse_detail_page(soup)
        return self._apply_details_to_listing(listing, details)

    def _scroll_detail_page(self):
        try:
            for _ in range(5):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(400,700)});"
                )
                time.sleep(random.uniform(0.3, 0.6))
            self.driver.execute_script("window.scrollTo(0,0);")
            time.sleep(0.3)
        except Exception:
            pass

    def _parse_detail_page(self, soup):
        details = {}
        for field_name, selector in self.SELECTORS['detail'].items():
            try:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(" ", strip=True)
                    if text:
                        details[field_name] = text
            except Exception:
                continue
        return details

    def _apply_details_to_listing(self, listing, details):
        if 'price_change' in details:
            listing.metadata.price_change = details['price_change']
        if 'property_type' in details:
            listing.features.property_type = self._parse_property_type(
                details['property_type']
            )
        if 'building_type' in details:
            parsed = self._parse_property_type(details['building_type'])
            if parsed != PropertyType.OTHER:
                listing.features.property_type = parsed
        if 'storeys' in details:
            listing.features.total_floors = self._parse_int(details['storeys'])
        if 'year_built' in details:
            listing.features.year_built = self._parse_int(details['year_built'])
        if 'neighbourhood' in details:
            if not listing.address.city or listing.address.city == "Unknown":
                listing.address.city = details['neighbourhood']
            listing.neighbourhood = details['neighbourhood']
        if 'time_on_realtor' in details:
            listing.metadata.time_on_site = details['time_on_realtor']
            pd = self._parse_time_on_realtor(details['time_on_realtor'])
            if pd:
                listing.metadata.posted_date = pd
        if 'heating_type' in details:
            listing.features.heating_type = self._parse_heating_type(
                details['heating_type']
            )
        if 'cooling' in details:
            cl = details['cooling'].lower()
            listing.features.air_conditioning = (
                any(t in cl for t in ('central','air','a/c','ac','cooling','yes'))
                and 'none' not in cl
            )
        parking_text = (
            details.get('parking_type')
            or details.get('parking_type_summary')
            or ''
        )
        if parking_text:
            listing.features.parking_type = self._parse_parking_type(parking_text)
        if 'total_parking' in details:
            listing.features.parking_spots = self._parse_int(
                details['total_parking']
            ) or 0
        if 'pool_type' in details:
            pl = details['pool_type'].lower()
            listing.amenities.pool = 'none' not in pl and pl not in (
                '', 'n/a', 'no', '-'
            )
        if 'features' in details:
            fl = details['features'].lower()
            if 'washer' in fl or 'laundry' in fl:
                if 'in-unit' in fl or 'in unit' in fl:
                    listing.features.laundry = LaundryType.IN_UNIT
                elif 'hook' in fl:
                    listing.features.laundry = LaundryType.HOOKUPS
                else:
                    listing.features.laundry = LaundryType.IN_BUILDING
            if 'balcony' in fl or 'patio' in fl:
                listing.features.balcony = True
            if 'dishwasher' in fl:
                listing.amenities.dishwasher = True
            listing.amenities.other_amenities = [
                f.strip() for f in details['features'].split(',') if f.strip()
            ]
        if 'style' in details:
            listing.features.style = details['style']
        if 'water' in details:
            wl = details['water'].lower()
            if 'included' in wl or 'municipal' in wl:
                listing.price.water_included = True
        if 'sewer' in details:
            listing.utilities_sewer = details['sewer']
        if 'amenities_nearby' in details:
            listing.amenities_nearby = details['amenities_nearby']
        if 'description' in details:
            listing.description = details['description']
        return listing

    # ── Search & filter setup ────────────────────────────────────

    def search_city(self, city_name):
        try:
            self.navigate(self.BASE_URL)
            self._dismiss_popups()
            self.short_delay()

            if not self._perform_search(city_name):
                return False
            self.medium_delay()
            self._dismiss_popups()

            self._switch_to_list_view()
            self.medium_delay()

            self._select_dropdown_option(
                self.SELECTORS['transaction_type'],
                self.TRANSACTION_TYPES['rent'],
            )
            self._wait_for_results_reload()

            if self.min_price:
                self._set_price_filter(self.SELECTORS['min_price'], self.min_price)
                self._wait_for_results_reload()
            if self.max_price:
                self._set_price_filter(self.SELECTORS['max_price'], self.max_price)
                self._wait_for_results_reload()
            if self.min_beds is not None:
                opt = self._format_beds_baths(self.min_beds, self.max_beds)
                self._select_dropdown_option(self.SELECTORS['beds'], opt)
                self._wait_for_results_reload()
            if self.min_baths is not None:
                opt = self._format_beds_baths(self.min_baths, self.max_baths)
                self._select_dropdown_option(self.SELECTORS['baths'], opt)
                self._wait_for_results_reload()

            self._select_dropdown_option(
                self.SELECTORS['sort_by'],
                self.SORT_OPTIONS['newest'],
            )
            self._wait_for_results_reload()
            self.medium_delay()

            return self._has_listings()
        except Exception as exc:
            logger.error(f"Error searching for {city_name}: {exc}")
            import traceback; traceback.print_exc()
            return False

    def _perform_search(self, city_name):
        try:
            si = self.wait_for_element(By.CSS_SELECTOR, self.SELECTORS['search_input'])
            si.clear()
            self.short_delay()
            self.type_slowly(si, f"{city_name}, QC")
            time.sleep(1.5)
            si.send_keys(Keys.RETURN)
            self.delay(self.PAGE_LOAD_DELAY)
            return True
        except Exception as exc:
            logger.error(f"Search failed: {exc}")
            return False

    def _switch_to_list_view(self):
        try:
            sel = self.find_element_safe(
                By.CSS_SELECTOR, self.SELECTORS['selected_toggle']
            )
            if sel and 'list' in sel.text.lower():
                return True
            btn = self.wait_for_clickable(
                By.CSS_SELECTOR, self.SELECTORS['list_view_button']
            )
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
            self.short_delay()
            try:
                btn.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", btn)
            self.delay(self.PAGE_LOAD_DELAY)
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS['listing_container_list'],
                timeout=10,
            )
            return True
        except Exception as exc:
            logger.error(f"List-view switch error: {exc}")
            return False

    def _wait_for_results_reload(self):
        self.delay((2.5, 4.5))
        try:
            self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS['listing_container_list'],
                timeout=10,
            )
        except TimeoutException:
            pass

    # ── Dropdown helpers ─────────────────────────────────────────

    def _open_dropdown(self, selector):
        try:
            c = self.wait_for_clickable(By.CSS_SELECTOR, selector)
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", c
            )
            time.sleep(random.uniform(0.3, 0.7))
            try:
                c.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", c)
            time.sleep(random.uniform(0.4, 0.8))
            return True
        except Exception as exc:
            logger.error(f"Cannot open dropdown {selector}: {exc}")
            return False

    def _close_dropdown(self):
        try:
            self.driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass

    def _get_visible_options(self, timeout=5):
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, self.SELECTORS['dropdown_options'])
                )
            )
            return [
                o for o in self.driver.find_elements(
                    By.CSS_SELECTOR, self.SELECTORS['dropdown_options']
                )
                if o.is_displayed()
            ]
        except TimeoutException:
            return []

    def _select_dropdown_option(self, container_sel, option_text):
        if not self._open_dropdown(container_sel):
            return False
        opts = self._get_visible_options()
        if not opts:
            self._close_dropdown()
            return False
        target = option_text.lower().strip()
        for o in opts:
            try:
                if o.text.strip().lower() == target:
                    o.click(); return True
            except StaleElementReferenceException:
                continue
        for o in opts:
            try:
                if target in o.text.strip().lower():
                    o.click(); return True
            except StaleElementReferenceException:
                continue
        self._close_dropdown()
        return False

    def _set_price_filter(self, container_sel, price):
        if not self._open_dropdown(container_sel):
            return False
        si = None
        try:
            inputs = WebDriverWait(self.driver, 5).until(
                lambda d: d.find_elements(
                    By.CSS_SELECTOR, self.SELECTORS['dropdown_search_input']
                )
            )
            si = next((e for e in inputs if e.is_displayed()), None)
        except TimeoutException:
            pass
        if not si:
            self._close_dropdown(); return False
        try:
            si.send_keys(Keys.CONTROL, 'a')
            si.send_keys(Keys.DELETE)
            time.sleep(0.2)
            self.type_slowly(si, str(price))
            time.sleep(random.uniform(0.3, 0.6))
            si.send_keys(Keys.RETURN)
            time.sleep(0.4)
            return True
        except Exception:
            self._close_dropdown()
            return False

    @staticmethod
    def _format_beds_baths(min_v, max_v):
        if min_v is None or min_v == 0:
            return "Any"
        if max_v is not None and max_v == min_v:
            return str(min_v)
        return f"{min_v}+"

    # ── Page extraction ──────────────────────────────────────────

    def _has_listings(self):
        try:
            c = self.wait_for_element(
                By.CSS_SELECTOR, self.SELECTORS['listing_container_list'],
                timeout=10,
            )
            cards = c.find_elements(By.CSS_SELECTOR, 'div.cardCon')
            logger.info(f"{len(cards)} listing cards visible")
            return len(cards) > 0
        except TimeoutException:
            return False

    def get_listings_from_page(self):
        """Parse every card on the current page.

        Each run is independent — there is no freshness check.  All cards
        are collected and pagination continues until ``max_pages`` is
        reached or no more pages exist.
        """
        listings = []
        self._scroll_through_results()
        self.short_delay()

        soup = BeautifulSoup(self.get_page_source(), 'lxml')
        container = soup.select_one(self.SELECTORS['listing_container_list'])
        if not container:
            return listings

        wrappers = container.select(':scope > div.cardCon')
        logger.info(f"Found {len(wrappers)} card wrappers")

        for i, wrapper in enumerate(wrappers):
            card = wrapper.select_one(':scope > div')
            if not card:
                # Some wrappers (ads, promoted slots) lack the inner
                # <div>.  Fall back to treating the wrapper itself as
                # the card so the link/address selectors still run.
                logger.debug(
                    f"Card {i+1}/{len(wrappers)}: no child <div>, "
                    f"using wrapper (classes={wrapper.get('class', [])})"
                )
                card = wrapper

            try:
                listing = self._parse_listing_card(card)
                if listing is None:
                    logger.debug(
                        f"Card {i+1}/{len(wrappers)}: "
                        f"parse returned None (no link or address)"
                    )
                    continue
                if listing.id in self._seen_ids:
                    logger.debug(
                        f"Card {i+1}/{len(wrappers)}: duplicate ID "
                        f"({listing.address.full_address[:40]})"
                    )
                    continue
                self._seen_ids.add(listing.id)
                listings.append(listing)
                if listing.address.city:
                    self._seen_cities.add(listing.address.city)
            except Exception as exc:
                logger.debug(f"Card {i+1}/{len(wrappers)} parse error: {exc}")

        logger.info(f"Extracted {len(listings)} listings from page")
        return listings

    def _scroll_through_results(self):
        try:
            last = 0
            for _ in range(20):
                self.driver.execute_script(
                    f"window.scrollBy(0, {random.randint(350,600)});"
                )
                time.sleep(random.uniform(0.25, 0.5))
                cur = self.driver.execute_script("return window.pageYOffset")
                if cur == last:
                    break
                last = cur
            footer = self.find_element_safe(
                By.CSS_SELECTOR, self.SELECTORS['pagination_container']
            )
            if footer:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", footer
                )
                time.sleep(random.uniform(0.5, 1.0))
            self.driver.execute_script("window.scrollTo(0,0);")
            time.sleep(0.4)
        except Exception:
            pass

    def _parse_listing_card(self, card):
        # ── Find the detail link ─────────────────────────────────
        link_el = card.select_one(self.SELECTORS['card_link'])
        if not link_el:
            link_el = card.select_one('a[href*="/real-estate/"]')
        if not link_el:
            # Broadest fallback — any anchor with an href
            link_el = card.select_one('a[href]')
        href = link_el.get('href', '') if link_el else ''
        if not href:
            return None
        url = href if href.startswith('http') else self.BASE_URL + href

        price_el = card.select_one(self.SELECTORS['card_price'])
        base_rent = self._parse_price(
            price_el.get_text(strip=True) if price_el else ''
        )

        addr_el = card.select_one(self.SELECTORS['card_address'])
        addr_text = addr_el.get_text(' ', strip=True) if addr_el else ''
        if not addr_text:
            # Try the link text as a last resort
            addr_text = link_el.get_text(' ', strip=True) if link_el else ''
        if not addr_text:
            return None

        icon_strip = card.select_one(self.SELECTORS['card_icon_strip'])
        beds, baths, sqft = self._parse_icon_strip(icon_strip)

        img_el = card.select_one(self.SELECTORS['card_image'])
        img_url = (
            (img_el.get('src') or img_el.get('data-src') or '')
            if img_el else ''
        )

        source_id = self._extract_source_id(url) or str(abs(hash(url)))[:12]
        sn, street, unit, city, prov = self._parse_address_components(addr_text)

        address = Address(
            full_address=addr_text,
            street_number=sn,
            street_name=street,
            unit_number=unit,
            city=city,
            province=prov,
            country='Canada',
        )

        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)

        return RentalListing(
            id=lid,
            address=address,
            price=PriceInfo(base_rent=base_rent or 0, currency='CAD'),
            features=PropertyFeatures(
                bedrooms=beds, bathrooms=baths, square_feet=sqft,
                property_type=PropertyType.APARTMENT,
            ),
            amenities=Amenities(),
            metadata=ListingMetadata(
                source_site=self.SITE_NAME,
                source_url=url,
                source_id=source_id,
                photo_urls=[img_url] if img_url else [],
            ),
            title=addr_text,
        )

    def _parse_icon_strip(self, icon_strip):
        beds = baths = sqft = None
        if not icon_strip:
            return beds, baths, sqft
        icons = icon_strip.select(':scope > div')
        for i, icon in enumerate(icons):
            num_el = icon.select_one('.listingCardIconNum')
            if not num_el:
                continue
            text = num_el.get_text(strip=True)
            if i == 0:   beds  = self._parse_bedrooms(text)
            elif i == 1: baths = self._parse_bathrooms(text)
            elif i == 2: sqft  = self._parse_sqft(text)
        return beds, baths, sqft

    def _parse_address_components(self, text):
        parts = [p.strip() for p in text.split(',') if p.strip()]
        street_line = parts[0] if parts else text
        province = 'QC'
        city = ''
        unit = None

        if len(parts) >= 2:
            province = self._normalize_province(parts[-1])
        if len(parts) >= 2:
            city = parts[-2]
        if len(parts) > 3:
            unit = ' '.join(parts[1:-2]).lstrip('#').strip() or None
        elif len(parts) == 3 and parts[1].startswith('#'):
            unit = parts[1].lstrip('#').strip() or None

        street_number = None
        street_name = street_line
        if street_line:
            idx = street_line.find(' ')
            if idx > 0 and street_line[:idx][0].isdigit():
                street_number = street_line[:idx]
                street_name = street_line[idx+1:].strip()

        return street_number, street_name, unit, city, province

    @staticmethod
    def _normalize_province(text):
        mapping = {
            'quebec':'QC','québec':'QC','qc':'QC',
            'ontario':'ON','on':'ON',
            'british columbia':'BC','bc':'BC',
            'alberta':'AB','ab':'AB',
        }
        return mapping.get(text.strip().lower(), text.strip().upper()[:2])

    # ── Pagination ───────────────────────────────────────────────

    def go_to_next_page(self):
        try:
            footer = self.find_element_safe(
                By.CSS_SELECTOR, self.SELECTORS['pagination_container']
            )
            if footer:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", footer
                )
                self.short_delay()
            btn = self.find_element_safe(
                By.CSS_SELECTOR, self.SELECTORS['next_page']
            )
            if not btn or not btn.is_displayed():
                return False
            cls = (btn.get_attribute('class') or '').lower()
            if 'disabled' in cls or btn.get_attribute('aria-disabled') == 'true':
                return False

            old_sig = self._first_card_sig()
            try:
                btn.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", btn)
            self._wait_for_page_change(old_sig)
            self.delay(self.PAGE_LOAD_DELAY)
            return self._has_listings()
        except Exception:
            return False

    def _first_card_sig(self):
        try:
            el = self.driver.find_element(
                By.CSS_SELECTOR,
                f"{self.SELECTORS['card_wrapper']} {self.SELECTORS['card_address']}",
            )
            return el.text.strip()
        except Exception:
            return ''

    def _wait_for_page_change(self, old_sig, timeout=12):
        if not old_sig:
            time.sleep(2); return
        end = time.time() + timeout
        while time.time() < end:
            if self._first_card_sig() != old_sig:
                return
            time.sleep(0.5)

    # ── Parsing helpers ──────────────────────────────────────────

    def _parse_price(self, text):
        if not text: return None
        m = re.search(r'\$?([\d,]+)', text.replace(' ',''))
        return float(m.group(1).replace(',','')) if m else None

    def _parse_bedrooms(self, text):
        if not text: return None
        s = text.lower().strip()
        if s in ('studio','bachelor','0'): return 0
        if '+' in s:
            try: return sum(int(p) for p in s.split('+') if p.strip().isdigit())
            except ValueError: pass
        m = re.search(r'(\d+)', s)
        return int(m.group(1)) if m else None

    def _parse_bathrooms(self, text):
        if not text: return None
        s = text.replace('½','.5').replace('1/2','.5')
        m = re.search(r'([\d.]+)', s)
        return float(m.group(1)) if m else None

    def _parse_sqft(self, text):
        if not text: return None
        s = re.sub(r'[^\d]','', text)
        return int(s) if s else None

    def _extract_source_id(self, url):
        m = re.search(r'/(\d{6,})', url or '')
        return m.group(1) if m else None

    def _parse_property_type(self, text):
        if not text: return PropertyType.APARTMENT
        s = text.lower()
        for k, v in {
            'apartment':PropertyType.APARTMENT, 'condo':PropertyType.CONDO,
            'condominium':PropertyType.CONDO, 'house':PropertyType.HOUSE,
            'townhouse':PropertyType.TOWNHOUSE, 'townhome':PropertyType.TOWNHOUSE,
            'row':PropertyType.TOWNHOUSE, 'duplex':PropertyType.DUPLEX,
            'triplex':PropertyType.TRIPLEX, 'studio':PropertyType.STUDIO,
            'bachelor':PropertyType.STUDIO, 'loft':PropertyType.LOFT,
            'basement':PropertyType.BASEMENT, 'room':PropertyType.ROOM,
        }.items():
            if k in s: return v
        return PropertyType.OTHER

    def _parse_heating_type(self, text):
        if not text: return HeatingType.UNKNOWN
        s = text.lower()
        for k, v in {
            'electric':HeatingType.ELECTRIC, 'gas':HeatingType.GAS,
            'oil':HeatingType.OIL, 'hydronic':HeatingType.HYDRONIC,
            'hot water':HeatingType.HYDRONIC, 'radiant':HeatingType.RADIANT,
            'forced air':HeatingType.FORCED_AIR, 'heat pump':HeatingType.HEAT_PUMP,
            'baseboard':HeatingType.BASEBOARD, 'central':HeatingType.CENTRAL,
        }.items():
            if k in s: return v
        return HeatingType.UNKNOWN

    def _parse_parking_type(self, text):
        if not text: return None
        lo = text.lower()
        if 'underground' in lo: return ParkingType.UNDERGROUND
        if 'indoor' in lo or 'garage' in lo or 'attached' in lo:
            return ParkingType.INDOOR
        if 'outdoor' in lo or 'surface' in lo: return ParkingType.OUTDOOR
        if 'street' in lo: return ParkingType.STREET
        if 'none' in lo: return ParkingType.NONE
        return ParkingType.OUTDOOR

    def _parse_time_on_realtor(self, text):
        if not text: return None
        lo = text.lower().strip()
        now = datetime.now()
        try:
            if 'day' in lo:
                m = re.search(r'(\d+)', lo)
                d = int(m.group(1)) if m else (0 if '<' in lo else 1)
                return datetime(now.year, now.month, now.day) - timedelta(days=d)
            if 'week' in lo:
                m = re.search(r'(\d+)', lo)
                return datetime(now.year, now.month, now.day) - timedelta(
                    weeks=int(m.group(1)) if m else 1
                )
            if 'month' in lo:
                m = re.search(r'(\d+)', lo)
                return datetime(now.year, now.month, now.day) - timedelta(
                    days=(int(m.group(1)) if m else 1) * 30
                )
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_int(text):
        if not text: return None
        m = re.search(r'(\d+)', text.replace(',',''))
        return int(m.group(1)) if m else None