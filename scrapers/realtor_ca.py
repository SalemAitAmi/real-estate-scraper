"""
Realtor.ca scraper — single selection paths, catalog-on-miss.
Includes on-site inquiry form support.
"""

import copy, logging, random, re, time
from typing import List, Optional, Dict

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, ElementClickInterceptedException,
    StaleElementReferenceException,
)

from data.models import (
    Address, Amenities, ListingMetadata, PriceInfo,
    PropertyFeatures, PropertyType, LaundryType,
    RentalListing, RentValue,
)
from .base_scraper import (
    BaseScraper, parse_price, parse_beds, parse_baths, parse_sqft,
    parse_int_val, parse_property_type, parse_heating_type,
    parse_parking_type, parse_time_on_site, apply_amenity_flag,
)

logger = logging.getLogger(__name__)


class RealtorCaScraper(BaseScraper):
    SITE_NAME = "realtor.ca"
    BASE_URL = "https://www.realtor.ca"

    SELECTORS = {
        "search_input":          "#homeSearchTxt",
        "list_view_button":      "#mapViewToggle > div > div > a:nth-child(2)",
        "selected_toggle":       "#mapViewToggle > div > div > a.toggleOption.selected",
        "transaction_type":      "#ddlTransactionTypeTopRes-container",
        "min_price":             "#ddlMinRentTop-container",
        "max_price":             "#ddlMaxRentTop-container",
        "beds":                  "#ddlBedsTop-container",
        "baths":                 "#ddlBathsTop-container",
        "sort_by":               "#ddlListResultsSort-container",
        "dropdown_search_input": (
            "span.select2-search.select2-search--dropdown "
            "> input.select2-search__field"),
        "dropdown_options":      "li.select2-results__option",
        "next_page": (
            "#ListViewPagination_Bottom > div "
            "> a.lnkNextResultsPage.paginationLink"
            ".paginationLinkForward.btn.small"),
        "pagination_container":  "#ListViewPagination_Bottom",
        "listing_container":     "#listInnerCon",
        "card_wrapper":          "#listInnerCon > div.cardCon",
        "card_link":             ":scope > a",
        "card_price":            ".listingCardPrice",
        "card_address":          ".listingCardAddress",
        "card_icon_strip":       ".listingCardIconStrip",
        "card_icon_num":         ".listingCardIconNum",
        "card_image":            ".listingCardImageCon img",

        "detail": {
            "price_change":        "#listingDetailsTopInnerCon > div.leftTableCell > div.PriceChangeOnRealtorCon.tag.priceChangeOnRealtorTag",
            "property_type":       "#propertyDetailsSectionContentSubCon_PropertyType > div.propertyDetailsSectionContentValue",
            "building_type":       "#propertyDetailsSectionContentSubCon_BuildingType > div.propertyDetailsSectionContentValue",
            "storeys":             "#propertyDetailsSectionContentSubCon_Stories > div.propertyDetailsSectionContentValue",
            "neighbourhood":       "#propertyDetailsSectionContentSubCon_NeighborhoodName > div.propertyDetailsSectionContentValue",
            "year_built":          "#propertyDetailsSectionContentSubCon_BuiltIn > div.propertyDetailsSectionContentValue",
            "parking_type_summary":"#propertyDetailsSectionContentSubCon_ParkingType > div.propertyDetailsSectionContentValue",
            "time_on_realtor":     "#propertyDetailsSectionContentSubCon_TimeOnRealtor > div.propertyDetailsSectionContentValue",
            "features":            "#propertyDetailsSectionVal_Features > div.propertyDetailsSectionContentValue",
            "style":               "#propertyDetailsSectionVal_Style > div.propertyDetailsSectionContentValue",
            "cooling":             "#propertyDetailsSectionVal_Cooling > div.propertyDetailsSectionContentValue",
            "heating_type":        "#propertyDetailsSectionVal_HeatingType > div.propertyDetailsSectionContentValue",
            "sewer":               "#propertyDetailsSectionVal_UtilitySewer > div.propertyDetailsSectionContentValue",
            "water":               "#propertyDetailsSectionVal_UtilityWater > div.propertyDetailsSectionContentValue",
            "pool_type":           "#propertyDetailsSectionVal_PoolType > div.propertyDetailsSectionContentValue",
            "amenities_nearby":    "#propertyDetailsSectionVal_AmenitiesNearby > div.propertyDetailsSectionContentValue",
            "parking_type":        "#propertyDetailsSectionVal_ParkingType > div.propertyDetailsSectionContentValue",
            "total_parking":       "#propertyDetailsSectionVal_TotalParkingSpaces > div.propertyDetailsSectionContentValue",
            "description":         "#listingDescriptionCon",
            "contact_name":        "#listingAgentName",
            "contact_phone":       "#listingAgentPhone a[href^='tel:']",
            "contact_email":       "#listingAgentEmail a[href^='mailto:']",
        },

        # ── On-site inquiry form ─────────────────────────────────
        "inquiry": {
            "email_button":       "div[id^='RealtorCard'] .realtorCardBottomRight a",
            "first_name":         "#FirstNameTxt",
            "last_name":          "#LastNameTxt",
            "email":              "#EmailAddressTxt",
            "message":            "#txtMessage",
            "location_check":     "#chkLocation",
            "recaptcha_iframe":   "iframe[title='reCAPTCHA']",
            "recaptcha_checkbox": "#recaptcha-anchor",
            "send_button":        "#btnEmailRealtorSend",
        },
    }

    TRANSACTION_TYPES = {"rent": "For rent"}
    SORT_OPTIONS = {"newest": "Newest"}

    # ════════════════════════════════════════════════════════════════
    #  Enrichment
    # ════════════════════════════════════════════════════════════════

    def enrich_listings(self, listings):
        if not listings: return listings
        enriched = []
        for i, l in enumerate(listings):
            logger.info("Fetching details [%d/%d]: %s",
                        i + 1, len(listings), l.address.full_address[:50])
            try: enriched.append(self._fetch_details(l))
            except Exception as exc:
                logger.error("Detail error for %s: %s", l.id, exc)
                enriched.append(l)
            if i < len(listings) - 1: self.delay((3.0, 6.0))
        return enriched

    def _fetch_details(self, listing):
        if not listing.metadata.source_url: return listing
        self.navigate(listing.metadata.source_url)
        self.dismiss_popups(); self.short_delay()
        self.scroll_page(steps=5, lo=400, hi=700)
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        return self._apply_details(listing, soup)

    def _apply_details(self, l, soup):
        d = {name: self.soup_text(soup, f"detail.{name}")
             for name in self.SELECTORS["detail"]}

        if d["price_change"]:     l.metadata.price_change = d["price_change"]
        if d["style"]:            l.features.style = d["style"]
        if d["sewer"]:            l.utilities_sewer = d["sewer"]
        if d["amenities_nearby"]: l.amenities_nearby = d["amenities_nearby"]
        if d["description"]:      l.description = d["description"]

        if d["property_type"]:
            l.features.property_type = parse_property_type(d["property_type"])
        if d["building_type"]:
            p = parse_property_type(d["building_type"])
            if p != PropertyType.OTHER: l.features.property_type = p
        if d["storeys"]:    l.features.total_floors = parse_int_val(d["storeys"])
        if d["year_built"]: l.features.year_built = parse_int_val(d["year_built"])
        if d["heating_type"]:
            l.features.heating_type = parse_heating_type(d["heating_type"])
        if d["total_parking"]:
            l.features.parking_spots = parse_int_val(d["total_parking"]) or 0

        if d["neighbourhood"]:
            l.neighbourhood = d["neighbourhood"]
            if not l.address.city or l.address.city == "Unknown":
                l.address.city = d["neighbourhood"]
        if d["time_on_realtor"]:
            l.metadata.time_on_site = d["time_on_realtor"]
            pd = parse_time_on_site(d["time_on_realtor"])
            if pd: l.metadata.posted_date = pd
        if d["cooling"]:
            cl = d["cooling"].lower()
            l.features.air_conditioning = (
                any(t in cl for t in ("central", "air", "a/c", "cooling", "yes"))
                and "none" not in cl)
        p_text = d["parking_type"] or d["parking_type_summary"]
        if p_text: l.features.parking_type = parse_parking_type(p_text)
        if d["pool_type"]:
            pl = d["pool_type"].lower()
            l.amenities.pool = "none" not in pl and pl not in ("", "n/a", "no", "-")
        if d["features"]:
            fl = d["features"].lower()
            if "washer" in fl or "laundry" in fl:
                if "in-unit" in fl or "in unit" in fl:
                    l.features.laundry = LaundryType.IN_UNIT
                elif "hook" in fl:
                    l.features.laundry = LaundryType.HOOKUPS
                else:
                    l.features.laundry = LaundryType.IN_BUILDING
            if "balcony" in fl or "patio" in fl: l.features.balcony = True
            if "dishwasher" in fl: l.amenities.dishwasher = True
            l.amenities.other_amenities = [
                f.strip() for f in d["features"].split(",") if f.strip()]
        if d["water"]:
            if "included" in d["water"].lower() or "municipal" in d["water"].lower():
                l.price.water_included = True
        if d["contact_name"]:  l.metadata.contact_name = d["contact_name"]
        phone_el = self.soup_el(soup, "detail.contact_phone")
        if phone_el:
            l.metadata.contact_phone = phone_el.get("href", "").replace("tel:", "")
        email_el = self.soup_el(soup, "detail.contact_email")
        if email_el:
            l.metadata.contact_email = email_el.get("href", "").replace("mailto:", "")
        return l

    # ════════════════════════════════════════════════════════════════
    #  On-site inquiry
    # ════════════════════════════════════════════════════════════════

    def send_inquiry(self, listing, *, first_name, last_name,
                     email, auto_send=False, **kwargs) -> bool:
        """Fill the realtor.ca contact form.

        If *auto_send* is True the Send button is clicked automatically.
        Otherwise the form is left populated and the browser stays open
        so the user can review and submit the draft manually.
        """
        url = listing.metadata.source_url
        if not url:
            logger.warning("No URL for listing %s", listing.id)
            return False

        self.navigate(url)
        self.dismiss_popups()
        self.short_delay()
        self.scroll_page(steps=5, lo=400, hi=700)

        if not self.css_click("inquiry.email_button", timeout=10):
            logger.warning("Email button not found on %s", url)
            return False
        time.sleep(1.5)

        self.fill_input("inquiry.first_name", first_name, slow=True)
        self.fill_input("inquiry.last_name", last_name, slow=True)
        self.fill_input("inquiry.email", email, slow=True)

        addr = listing.address.full_address
        msg = (
            f"Hello,\n\n"
            f"My name is {first_name} {last_name}. I am writing to "
            f"inquire about the availability of the rental listing "
            f"at:\n\n{addr}\n\n"
            f"Could you please let me know if this unit is still "
            f"available and share any details on lease terms and "
            f"move-in date?\n\n"
            f"Thank you,\n{first_name} {last_name}"
        )
        self.fill_input("inquiry.message", msg)

        # "Give Location" is on by default → click once to disable
        self.css_click("inquiry.location_check")
        time.sleep(0.5)

        captcha_ok = self._handle_recaptcha()

        if auto_send:
            time.sleep(1)
            self.css_click("inquiry.send_button")
            time.sleep(3)
            logger.info("Inquiry auto-sent for %s", addr)
        else:
            logger.info(
                "Inquiry draft prepared for %s — review the browser "
                "and click Send to submit (captcha_ok=%s)",
                addr, captcha_ok)
            self._wait_for_manual_send()

        return True

    def _wait_for_manual_send(self, timeout=300):
        """Block until the Send button disappears (submitted) or the
        user closes the browser, up to *timeout* seconds."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                btn = self.driver.find_element(
                    By.CSS_SELECTOR, self._sel("inquiry.send_button"))
                if not btn.is_displayed():
                    logger.info("Manual inquiry submitted")
                    return
            except Exception:
                # Form gone or browser closed by the user
                logger.info("Inquiry form closed")
                return
            time.sleep(2)
        logger.info("Manual send wait timed out after %ds", timeout)

    def _handle_recaptcha(self, timeout=90) -> bool:
        """Switch into the reCAPTCHA iframe, click checkbox, wait.

        Returns ``True`` if the challenge resolved automatically.
        If a visual challenge appears the browser is visible so the
        user can solve it manually; the method polls until ``timeout``.
        """
        try:
            iframe = self.css("inquiry.recaptcha_iframe", timeout=5)
            if not iframe:
                return False
            self.driver.switch_to.frame(iframe)
            time.sleep(0.5)

            cb = self.css("inquiry.recaptcha_checkbox", timeout=5)
            if not cb:
                return False
            self.safe_click(cb)
            time.sleep(3)

            # Poll until solved or timeout
            end = time.time() + timeout
            while time.time() < end:
                try:
                    if cb.get_attribute("aria-checked") == "true":
                        logger.info("reCAPTCHA resolved")
                        return True
                except Exception:
                    pass
                time.sleep(2)
            logger.warning("reCAPTCHA not resolved within %ds", timeout)
            return False
        except Exception as exc:
            logger.warning("reCAPTCHA error: %s", exc)
            return False
        finally:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass

    # ════════════════════════════════════════════════════════════════
    #  search_city  (unchanged from previous refactor)
    # ════════════════════════════════════════════════════════════════

    def search_city(self, city_name):
        try:
            self.navigate(self.BASE_URL)
            self.dismiss_popups(); self.short_delay()
            si = self.css("search_input", timeout=10)
            if not si: return False
            si.clear(); self.short_delay()
            self.type_slowly(si, f"{city_name}, QC")
            time.sleep(1.5); si.send_keys(Keys.RETURN)
            self.delay(self.PAGE_LOAD_DELAY); self.dismiss_popups()
            self._switch_to_list_view(); self.medium_delay()
            self._select_dd("transaction_type", "For rent"); self._wait_results()
            if self.min_price:
                self._set_price("min_price", self.min_price); self._wait_results()
            if self.max_price:
                self._set_price("max_price", self.max_price); self._wait_results()
            if self.min_beds is not None:
                self._select_dd("beds", self._fmt_range(self.min_beds, self.max_beds))
                self._wait_results()
            if self.min_baths is not None:
                self._select_dd("baths", self._fmt_range(self.min_baths, self.max_baths))
                self._wait_results()
            self._select_dd("sort_by", "Newest"); self._wait_results()
            self.medium_delay()
            return self._has_listings()
        except Exception as exc:
            logger.error("Error searching for %s: %s", city_name, exc)
            return False

    def _switch_to_list_view(self):
        sel = self.css("selected_toggle")
        if sel and "list" in sel.text.lower(): return
        btn = self.css("list_view_button", timeout=8)
        if not btn: return
        self.scroll_to_element(btn); self.safe_click(btn)
        self.delay(self.PAGE_LOAD_DELAY)

    def _wait_results(self):
        self.delay((2.5, 4.5)); self.css("listing_container", timeout=10)

    def _open_dd(self, name):
        el = self.css(name, timeout=5)
        if not el: return False
        self.scroll_to_element(el); time.sleep(random.uniform(0.3, 0.7))
        self.safe_click(el); time.sleep(random.uniform(0.4, 0.8))
        return True

    def _close_dd(self):
        try:
            self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except Exception: pass

    def _get_options(self, timeout=5):
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, self._sel("dropdown_options"))))
            return [o for o in self.driver.find_elements(
                By.CSS_SELECTOR, self._sel("dropdown_options"))
                if o.is_displayed()]
        except TimeoutException: return []

    def _select_dd(self, container_name, option_text):
        if not self._open_dd(container_name): return False
        target = option_text.lower().strip()
        for o in self._get_options():
            try:
                if target in o.text.strip().lower(): o.click(); return True
            except StaleElementReferenceException: continue
        self._close_dd(); return False

    def _set_price(self, container_name, price):
        if not self._open_dd(container_name): return False
        try:
            inputs = WebDriverWait(self.driver, 5).until(
                lambda d: d.find_elements(
                    By.CSS_SELECTOR, self._sel("dropdown_search_input")))
            si = next((e for e in inputs if e.is_displayed()), None)
        except TimeoutException: si = None
        if not si: self._close_dd(); return False
        si.send_keys(Keys.CONTROL, "a"); si.send_keys(Keys.DELETE)
        time.sleep(0.2); self.type_slowly(si, str(price))
        time.sleep(0.4); si.send_keys(Keys.RETURN); time.sleep(0.4)
        return True

    @staticmethod
    def _fmt_range(min_v, max_v):
        if min_v is None or min_v == 0: return "Any"
        if max_v is not None and max_v == min_v: return str(min_v)
        return f"{min_v}+"

    def _has_listings(self):
        c = self.css("listing_container", timeout=10)
        if not c: return False
        cards = c.find_elements(By.CSS_SELECTOR, "div.cardCon")
        logger.info("%d listing cards visible", len(cards))
        return len(cards) > 0

    def get_listings_from_page(self):
        self.scroll_page(steps=20, lo=350, hi=600); self.short_delay()
        soup = BeautifulSoup(self.get_page_source(), "lxml")
        container = self.soup_el(soup, "listing_container")
        if not container: return []
        wrappers = container.select(":scope > div.cardCon")
        listings = []
        for i, wrapper in enumerate(wrappers):
            card = wrapper.select_one(":scope > div") or wrapper
            try:
                l = self._parse_card(card)
                if l:
                    listings.append(l)
                    if l.address.city: self._seen_cities.add(l.address.city)
            except Exception as exc:
                logger.debug("Card %d parse error: %s", i, exc)
        logger.info("Extracted %d listings from page", len(listings))
        return listings

    def _parse_card(self, card):
        link_el = card.select_one(self._sel("card_link"))
        if not link_el: link_el = card.select_one("a[href]")
        href = link_el.get("href", "") if link_el else ""
        if not href: return None
        url = href if href.startswith("http") else self.BASE_URL + href
        price_el = card.select_one(self._sel("card_price"))
        base_rent = parse_price(price_el.get_text(strip=True) if price_el else "")
        addr_el = card.select_one(self._sel("card_address"))
        addr_text = (addr_el.get_text(" ", strip=True) if addr_el
                     else link_el.get_text(" ", strip=True) if link_el else "")
        if not addr_text: return None
        beds, baths, sqft = self._parse_icons(
            card.select_one(self._sel("card_icon_strip")))
        img_el = card.select_one(self._sel("card_image"))
        img_url = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""
        source_id = self._source_id(url) or str(abs(hash(url)))[:12]
        sn, street, unit, city, prov = self._parse_addr_parts(addr_text)
        address = Address(full_address=addr_text, street_number=sn,
                          street_name=street, unit_number=unit,
                          city=city, province=prov, country="Canada")
        lid = RentalListing.generate_id(self.SITE_NAME, source_id, url)
        return RentalListing(
            id=lid, address=address,
            price=PriceInfo(base_rent=RentValue(amount=base_rent or 0), currency="CAD"),
            features=PropertyFeatures(bedrooms=beds, bathrooms=baths,
                                      square_feet=sqft,
                                      property_type=PropertyType.APARTMENT),
            amenities=Amenities(),
            metadata=ListingMetadata(source_site=self.SITE_NAME,
                                     source_url=url, source_id=source_id,
                                     photo_urls=[img_url] if img_url else []))

    def _parse_icons(self, strip):
        beds = baths = sqft = None
        if not strip: return beds, baths, sqft
        for i, icon in enumerate(strip.select(":scope > div")):
            num_el = icon.select_one(self._sel("card_icon_num"))
            if not num_el: continue
            text = num_el.get_text(strip=True)
            if i == 0:   beds = parse_beds(text)
            elif i == 1: baths = parse_baths(text)
            elif i == 2: sqft = parse_sqft(text)
        return beds, baths, sqft

    def _parse_addr_parts(self, text):
        parts = [p.strip() for p in text.split(",") if p.strip()]
        street_line = parts[0] if parts else text
        province = self._norm_prov(parts[-1]) if len(parts) >= 2 else "QC"
        city = parts[-2] if len(parts) >= 2 else ""
        unit = None
        if len(parts) > 3:
            unit = " ".join(parts[1:-2]).lstrip("#").strip() or None
        elif len(parts) == 3 and parts[1].startswith("#"):
            unit = parts[1].lstrip("#").strip() or None
        sn, sname = None, street_line
        if street_line:
            idx = street_line.find(" ")
            if idx > 0 and street_line[:idx][0].isdigit():
                sn, sname = street_line[:idx], street_line[idx + 1:].strip()
        return sn, sname, unit, city, province

    @staticmethod
    def _norm_prov(text):
        m = {"quebec": "QC", "québec": "QC", "qc": "QC",
             "ontario": "ON", "on": "ON", "british columbia": "BC", "bc": "BC",
             "alberta": "AB", "ab": "AB"}
        return m.get(text.strip().lower(), text.strip().upper()[:2])

    @staticmethod
    def _source_id(url):
        m = re.search(r"/(\d{6,})", url or "")
        return m.group(1) if m else None

    def go_to_next_page(self):
        try:
            pag = self.css("pagination_container")
            if pag: self.scroll_to_element(pag); self.short_delay()
            btn = self.css("next_page")
            if not btn or not btn.is_displayed(): return False
            cls = (btn.get_attribute("class") or "").lower()
            if "disabled" in cls or btn.get_attribute("aria-disabled") == "true":
                return False
            old_sig = self._first_sig()
            self.safe_click(btn); self._wait_page_change(old_sig)
            self.delay(self.PAGE_LOAD_DELAY); return self._has_listings()
        except Exception: return False

    def _first_sig(self):
        try:
            el = self.driver.find_element(
                By.CSS_SELECTOR,
                f"{self._sel('card_wrapper')} {self._sel('card_address')}")
            return el.text.strip()
        except Exception: return ""

    def _wait_page_change(self, old_sig, timeout=12):
        if not old_sig: time.sleep(2); return
        end = time.time() + timeout
        while time.time() < end:
            if self._first_sig() != old_sig: return
            time.sleep(0.5)