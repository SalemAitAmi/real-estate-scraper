"""
test_store_merge.py
───────────────────
Deterministic, fully-encapsulated tests for the scraper-dump → store
merge pipeline.

Run from the project root:

    python test_store_merge.py

Exit code 0 = all assertions passed.

Fixtures
────────
• SCRAPER_DUMP   — simulates raw scraper output (scraper-dump.json)
• PRE_STORE      — simulates an existing store.json before merge
• POST_STORE     — the expected state of store.json after merge

Three independent scenarios are tested, each with its own trio of
fixtures.  Every scenario is fully self-contained — no disk I/O, no
browser, no network.
"""

import copy
import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Project imports ───────────────────────────────────────────────
from data.models import (
    Address,
    Amenities,
    ListingMetadata,
    PriceInfo,
    PropertyFeatures,
    PropertyType,
    HeatingType,
    LaundryType,
    ParkingType,
    RentalListing,
)
from data.normalizer import normalize_listing, deduplicate_listings
from data.store import ListingStore, MergeReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("test-store-merge")

# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

_T0 = datetime(2026, 5, 1, 12, 0, 0)
_T1 = datetime(2026, 5, 8, 12, 0, 0)
_T2 = datetime(2026, 5, 15, 12, 0, 0)


def _listing(
    *,
    lid: str,
    source_site: str = "rentals.ca",
    source_id: str = "",
    source_url: str = "",
    full_address: str = "123 Test St, Montreal",
    city: str = "Montreal",
    province: str = "QC",
    base_rent: float = 1500.0,
    bedrooms: Optional[int] = 2,
    bathrooms: Optional[float] = 1.0,
    sqft: Optional[int] = 800,
    property_type: PropertyType = PropertyType.APARTMENT,
    heating_type: HeatingType = HeatingType.ELECTRIC,
    heating_included: bool = False,
    water_included: bool = False,
    electricity_included: bool = False,
    internet_included: bool = False,
    pets_allowed: bool = False,
    cats_allowed: Optional[bool] = None,
    dogs_allowed: Optional[bool] = None,
    balcony: bool = False,
    air_conditioning: bool = False,
    parking_type: Optional[ParkingType] = None,
    parking_spots: int = 0,
    laundry: LaundryType = LaundryType.NONE,
    dishwasher: bool = False,
    gym: bool = False,
    pool: bool = False,
    elevator: bool = False,
    title: str = "",
    description: str = "",
    photo_urls: Optional[List[str]] = None,
    contact_name: Optional[str] = None,
    contact_phone: Optional[str] = None,
    contact_email: Optional[str] = None,
    posted_date: Optional[datetime] = None,
    available_date: Optional[datetime] = None,
    is_selected: bool = False,
    is_discarded: bool = False,
    user_notes: str = "",
    email_thread_id: Optional[str] = None,
    has_unread_email: bool = False,
    first_seen: datetime = _T0,
    last_seen: datetime = _T0,
    times_seen: int = 1,
    adjusted_rent: Optional[float] = None,
    parking_fee: Optional[float] = None,
    security_deposit: Optional[float] = None,
    neighbourhood: Optional[str] = None,
    furnished: bool = False,
) -> RentalListing:
    """Convenience factory — every field has a sensible default."""
    return RentalListing(
        id=lid,
        address=Address(
            full_address=full_address,
            city=city,
            province=province,
            country="Canada",
        ),
        price=PriceInfo(
            base_rent=base_rent,
            currency="CAD",
            adjusted_rent=adjusted_rent,
            heating_included=heating_included,
            water_included=water_included,
            electricity_included=electricity_included,
            internet_included=internet_included,
            parking_fee=parking_fee,
            security_deposit=security_deposit,
        ),
        features=PropertyFeatures(
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            square_feet=sqft,
            property_type=property_type,
            heating_type=heating_type,
            pets_allowed=pets_allowed,
            cats_allowed=cats_allowed,
            dogs_allowed=dogs_allowed,
            balcony=balcony,
            air_conditioning=air_conditioning,
            parking_type=parking_type,
            parking_spots=parking_spots,
            laundry=laundry,
            furnished=furnished,
        ),
        amenities=Amenities(
            dishwasher=dishwasher,
            gym=gym,
            pool=pool,
            elevator=elevator,
        ),
        metadata=ListingMetadata(
            source_site=source_site,
            source_url=source_url or f"https://{source_site}/{source_id or lid}",
            source_id=source_id or lid,
            scraped_at=last_seen,
            posted_date=posted_date,
            available_date=available_date,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            photo_urls=photo_urls or [],
        ),
        title=title or full_address,
        description=description,
        is_selected=is_selected,
        is_discarded=is_discarded,
        user_notes=user_notes,
        email_thread_id=email_thread_id,
        has_unread_email=has_unread_email,
        first_seen=first_seen,
        last_seen=last_seen,
        times_seen=times_seen,
        neighbourhood=neighbourhood,
    )


def _make_store(listings: List[RentalListing]) -> ListingStore:
    """Build an in-memory store backed by a temp file (auto-cleaned)."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    if listings:
        json.dump([l.to_dict() for l in listings], tmp, indent=2)
    else:
        json.dump([], tmp)
    tmp.close()
    return ListingStore(store_path=Path(tmp.name))


def _run_pipeline(
    raw: List[RentalListing],
    pre: List[RentalListing],
) -> Tuple[ListingStore, MergeReport]:
    """Simulate the normalise → dedup → merge pipeline."""
    store = _make_store(pre)
    normalised = [normalize_listing(copy.deepcopy(l)) for l in raw]
    deduped = deduplicate_listings(normalised)
    report = store.merge_results(deduped)
    return store, report


# ════════════════════════════════════════════════════════════════════
#  Assertion helpers
# ════════════════════════════════════════════════════════════════════

_failures: List[str] = []


def _assert(condition: bool, msg: str):
    if not condition:
        _failures.append(msg)
        log.error(f"  FAIL: {msg}")
    else:
        log.info(f"  PASS: {msg}")


def _assert_eq(actual, expected, label: str):
    if actual != expected:
        _failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        log.error(f"  FAIL: {label}: expected {expected!r}, got {actual!r}")
    else:
        log.info(f"  PASS: {label} == {expected!r}")


def _assert_ne(actual, not_expected, label: str):
    if actual == not_expected:
        _failures.append(f"{label}: should not be {not_expected!r}")
        log.error(f"  FAIL: {label}: should not be {not_expected!r}")
    else:
        log.info(f"  PASS: {label} != {not_expected!r}")


def _assert_gte(actual, minimum, label: str):
    if actual < minimum:
        _failures.append(f"{label}: {actual!r} < {minimum!r}")
        log.error(f"  FAIL: {label}: {actual!r} < {minimum!r}")
    else:
        log.info(f"  PASS: {label} >= {minimum!r}")


def _assert_in(item, container, label: str):
    if item not in container:
        _failures.append(f"{label}: {item!r} not found")
        log.error(f"  FAIL: {label}: {item!r} not found")
    else:
        log.info(f"  PASS: {label}: {item!r} present")


# ════════════════════════════════════════════════════════════════════
#  SCENARIO 1 — Fresh scrape into an empty store
# ════════════════════════════════════════════════════════════════════

def scenario_1():
    log.info("\n" + "=" * 70)
    log.info("SCENARIO 1 — Fresh scrape into empty store")
    log.info("=" * 70)

    dump = [
        _listing(
            lid="aaa1",
            full_address="100 Sherbrooke W, Montreal",
            city="Montreal",
            base_rent=1800,
            bedrooms=2,
            bathrooms=1.0,
            sqft=900,
            balcony=True,
            gym=True,
            description="Bright corner unit with city views.",
            photo_urls=["https://example.com/img1.jpg"],
            contact_name="Alice Agent",
            first_seen=_T1,
            last_seen=_T1,
        ),
        _listing(
            lid="aaa2",
            full_address="200 St-Denis, Montreal",
            city="Montreal",
            base_rent=1400,
            bedrooms=1,
            bathrooms=1.0,
            sqft=650,
            pets_allowed=True,
            laundry=LaundryType.IN_BUILDING,
            first_seen=_T1,
            last_seen=_T1,
        ),
        _listing(
            lid="aaa3",
            full_address="50 Blvd Curé-Labelle, Laval",
            city="Laval",
            base_rent=1600,
            bedrooms=2,
            bathrooms=1.5,
            sqft=850,
            parking_type=ParkingType.UNDERGROUND,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # Duplicate of aaa2 (same id) — tests intra-batch dedup
        _listing(
            lid="aaa2",
            full_address="200 St-Denis, Montreal",
            city="Montreal",
            base_rent=1400,
            bedrooms=1,
            bathrooms=1.0,
            sqft=650,
            pets_allowed=True,
            laundry=LaundryType.IN_BUILDING,
            description="Now with a longer description for richness.",
            first_seen=_T1,
            last_seen=_T1,
        ),
    ]

    pre_store: List[RentalListing] = []

    store, report = _run_pipeline(dump, pre_store)

    log.info("\n  ── Report ──")
    _assert_eq(report.added, 3, "S1: 3 listings added")
    _assert_eq(report.updated, 0, "S1: 0 updated")
    _assert_eq(report.unchanged, 0, "S1: 0 unchanged")
    _assert_eq(report.skipped_quality, 0, "S1: 0 quality-skipped")

    log.info("\n  ── Store state ──")
    _assert_eq(len(store.listings), 3, "S1: store has 3 listings")
    _assert_in("aaa1", store.listings, "S1: aaa1 present")
    _assert_in("aaa2", store.listings, "S1: aaa2 present")
    _assert_in("aaa3", store.listings, "S1: aaa3 present")

    log.info("\n  ── Intra-batch dedup ──")
    aaa2 = store.listings["aaa2"]
    _assert(
        len(aaa2.description) > 10,
        "S1: dedup kept richer copy of aaa2 (has description)",
    )

    log.info("\n  ── Data integrity ──")
    aaa1 = store.listings["aaa1"]
    _assert_eq(aaa1.price.base_rent, 1800.0, "S1/aaa1: rent preserved")
    _assert_eq(aaa1.features.bedrooms, 2, "S1/aaa1: beds preserved")
    _assert_eq(aaa1.features.balcony, True, "S1/aaa1: balcony preserved")
    _assert_eq(aaa1.amenities.gym, True, "S1/aaa1: gym preserved")
    _assert_eq(
        aaa1.metadata.contact_name, "Alice Agent", "S1/aaa1: contact preserved"
    )
    _assert_eq(aaa1.address.city, "Montreal", "S1/aaa1: city normalised")

    aaa3 = store.listings["aaa3"]
    _assert_eq(
        aaa3.features.parking_type,
        ParkingType.UNDERGROUND,
        "S1/aaa3: parking preserved",
    )
    _assert_eq(aaa3.address.city, "Laval", "S1/aaa3: city preserved")


# ════════════════════════════════════════════════════════════════════
#  SCENARIO 2 — Re-scrape with changes, protections, and edge cases
# ════════════════════════════════════════════════════════════════════

def scenario_2():
    log.info("\n" + "=" * 70)
    log.info("SCENARIO 2 — Re-scrape: updates, protections, and edge cases")
    log.info("=" * 70)

    # ── Pre-existing store (simulates previous run) ──────────────
    pre_store = [
        # bbb1: user selected, has notes and email thread
        _listing(
            lid="bbb1",
            full_address="300 René-Lévesque, Montreal",
            city="Montreal",
            base_rent=2000,
            bedrooms=3,
            bathrooms=1.5,
            sqft=1100,
            balcony=True,
            air_conditioning=True,
            gym=True,
            pool=True,
            description="Spacious downtown condo with amenities.",
            contact_name="Bob Broker",
            contact_phone="514-555-0001",
            photo_urls=[
                "https://example.com/bbb1_1.jpg",
                "https://example.com/bbb1_2.jpg",
            ],
            is_selected=True,
            user_notes="Schedule visit next week",
            email_thread_id="thread_bbb1",
            has_unread_email=True,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=2,
            parking_type=ParkingType.UNDERGROUND,
            heating_type=HeatingType.ELECTRIC,
            heating_included=True,
            pets_allowed=True,
            cats_allowed=True,
            dogs_allowed=True,
        ),
        # bbb2: user discarded
        _listing(
            lid="bbb2",
            full_address="400 Sainte-Catherine, Montreal",
            city="Montreal",
            base_rent=1700,
            bedrooms=2,
            bathrooms=1.0,
            sqft=780,
            elevator=True,
            laundry=LaundryType.IN_BUILDING,
            is_discarded=True,
            user_notes="Too noisy",
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # bbb3: plain listing — will receive legitimate updates
        _listing(
            lid="bbb3",
            full_address="500 Mont-Royal E, Montreal",
            city="Montreal",
            base_rent=1500,
            bedrooms=2,
            bathrooms=1.0,
            sqft=750,
            description="Cozy Plateau apartment.",
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # bbb4: listing that will NOT appear in the new scrape (gone)
        _listing(
            lid="bbb4",
            full_address="600 Papineau, Montreal",
            city="Montreal",
            base_rent=1300,
            bedrooms=1,
            bathrooms=1.0,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # bbb5: will test zero-rent / empty-string protection
        _listing(
            lid="bbb5",
            full_address="700 Jean-Talon, Montreal",
            city="Montreal",
            base_rent=1900,
            bedrooms=2,
            bathrooms=1.0,
            sqft=880,
            description="Renovated with modern kitchen.",
            contact_name="Claire Contact",
            contact_phone="514-555-0005",
            photo_urls=["https://example.com/bbb5.jpg"],
            first_seen=_T0,
            last_seen=_T0,
            times_seen=3,
        ),
        # bbb6: will test monotonic booleans
        _listing(
            lid="bbb6",
            full_address="800 Saint-Urbain, Montreal",
            city="Montreal",
            base_rent=1650,
            bedrooms=2,
            bathrooms=1.0,
            sqft=800,
            dishwasher=True,
            gym=True,
            pool=True,
            balcony=True,
            air_conditioning=True,
            elevator=True,
            pets_allowed=True,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
    ]

    # ── Scraper dump (simulates new run) ─────────────────────────
    dump = [
        # bbb1 re-scraped: price changed, scraper lost some
        # fields (contact_phone=None, photos=[]) — protections
        # must keep originals.  User actions must survive.
        _listing(
            lid="bbb1",
            full_address="300 René-Lévesque, Montreal",
            city="Montreal",
            base_rent=2100,
            bedrooms=3,
            bathrooms=1.5,
            sqft=1100,
            balcony=True,
            air_conditioning=True,
            gym=True,
            pool=True,
            description="Spacious downtown condo with amenities.",
            contact_name="Bob Broker",
            contact_phone=None,
            photo_urls=[],
            parking_type=ParkingType.UNDERGROUND,
            heating_type=HeatingType.ELECTRIC,
            heating_included=True,
            pets_allowed=True,
            cats_allowed=True,
            dogs_allowed=True,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb2 re-scraped: unchanged — user discard must survive
        _listing(
            lid="bbb2",
            full_address="400 Sainte-Catherine, Montreal",
            city="Montreal",
            base_rent=1700,
            bedrooms=2,
            bathrooms=1.0,
            sqft=780,
            elevator=True,
            laundry=LaundryType.IN_BUILDING,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb3: legitimate updates — price drop, new details added
        _listing(
            lid="bbb3",
            full_address="500 Mont-Royal E, Montreal",
            city="Montreal",
            base_rent=1400,
            bedrooms=2,
            bathrooms=1.0,
            sqft=750,
            description="Cozy Plateau apartment. Recently renovated.",
            balcony=True,
            laundry=LaundryType.IN_UNIT,
            dishwasher=True,
            contact_name="Dave Details",
            contact_phone="514-555-0003",
            photo_urls=["https://example.com/bbb3_new.jpg"],
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb5: scraper error — returned zero rent, empty description,
        # empty contact, empty photos.  All must be blocked.
        _listing(
            lid="bbb5",
            full_address="700 Jean-Talon, Montreal",
            city="Montreal",
            base_rent=0,
            bedrooms=2,
            bathrooms=1.0,
            sqft=880,
            description="",
            contact_name="",
            contact_phone="",
            photo_urls=[],
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb6: scraper page lacked amenity flags — all False.
        # Monotonic truth must keep every True from the store.
        _listing(
            lid="bbb6",
            full_address="800 Saint-Urbain, Montreal",
            city="Montreal",
            base_rent=1650,
            bedrooms=2,
            bathrooms=1.0,
            sqft=800,
            dishwasher=False,
            gym=False,
            pool=False,
            balcony=False,
            air_conditioning=False,
            elevator=False,
            pets_allowed=False,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb7: brand-new listing in this run
        _listing(
            lid="bbb7",
            full_address="900 Atwater, Montreal",
            city="Montreal",
            base_rent=2200,
            bedrooms=3,
            bathrooms=2.0,
            sqft=1200,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # bbb8: quality-gate failure — almost empty listing
        _listing(
            lid="bbb8",
            source_url="",
            full_address="",
            city="",
            base_rent=0,
            bedrooms=None,
            bathrooms=None,
            sqft=None,
            first_seen=_T1,
            last_seen=_T1,
        ),
    ]

    store, report = _run_pipeline(dump, pre_store)

    # ── Report counts ────────────────────────────────────────────
    log.info("\n  ── Report ──")
    _assert_eq(report.added, 1, "S2: 1 new listing added (bbb7)")
    _assert(report.updated >= 2, "S2: at least bbb1,bbb3 updated")
    _assert(report.skipped_quality >= 1, "S2: bbb8 quality-gated")

    # ── Store size ───────────────────────────────────────────────
    log.info("\n  ── Store size ──")
    _assert_eq(
        len(store.listings), 7,
        "S2: 6 pre-existing + 1 new = 7 (bbb8 quality-gated, bbb4 untouched)",
    )

    # ── bbb1: user actions survive, price updated, protections ───
    log.info("\n  ── bbb1: user-action preservation + selective update ──")
    b1 = store.listings["bbb1"]
    _assert_eq(b1.is_selected, True, "S2/bbb1: is_selected preserved")
    _assert_eq(b1.user_notes, "Schedule visit next week", "S2/bbb1: notes preserved")
    _assert_eq(b1.email_thread_id, "thread_bbb1", "S2/bbb1: thread preserved")
    _assert_eq(b1.price.base_rent, 2100.0, "S2/bbb1: rent updated to 2100")
    _assert_eq(
        b1.metadata.contact_phone,
        "514-555-0001",
        "S2/bbb1: None phone did not wipe existing",
    )
    _assert_eq(
        len(b1.metadata.photo_urls),
        2,
        "S2/bbb1: empty photo list did not wipe existing",
    )
    _assert_gte(b1.times_seen, 3, "S2/bbb1: times_seen incremented")

    # ── bbb2: discarded user action preserved on unchanged re-scrape
    log.info("\n  ── bbb2: discard survives identical re-scrape ──")
    b2 = store.listings["bbb2"]
    _assert_eq(b2.is_discarded, True, "S2/bbb2: is_discarded preserved")
    _assert_eq(b2.user_notes, "Too noisy", "S2/bbb2: notes preserved")
    _assert_gte(b2.times_seen, 2, "S2/bbb2: times_seen incremented")

    # ── bbb3: legitimate updates applied ─────────────────────────
    log.info("\n  ── bbb3: legitimate field updates ──")
    b3 = store.listings["bbb3"]
    _assert_eq(b3.price.base_rent, 1400.0, "S2/bbb3: price updated to 1400")
    _assert(
        "renovated" in b3.description.lower(),
        "S2/bbb3: description updated",
    )
    _assert_eq(b3.features.balcony, True, "S2/bbb3: balcony set to True")
    _assert_eq(
        b3.features.laundry, LaundryType.IN_UNIT, "S2/bbb3: laundry updated"
    )
    _assert_eq(b3.amenities.dishwasher, True, "S2/bbb3: dishwasher set True")
    _assert_eq(
        b3.metadata.contact_name, "Dave Details", "S2/bbb3: contact updated"
    )
    _assert(len(b3.metadata.photo_urls) > 0, "S2/bbb3: photos added")

    # ── bbb4: not in scrape — untouched ──────────────────────────
    log.info("\n  ── bbb4: listing absent from scrape is untouched ──")
    b4 = store.listings["bbb4"]
    _assert_eq(b4.price.base_rent, 1300.0, "S2/bbb4: rent unchanged")
    _assert_eq(b4.times_seen, 1, "S2/bbb4: times_seen unchanged")

    # ── bbb5: zero-rent + empty-string protection ────────────────
    log.info("\n  ── bbb5: scraper-error protection ──")
    b5 = store.listings["bbb5"]
    _assert_eq(
        b5.price.base_rent,
        1900.0,
        "S2/bbb5: zero rent did not overwrite 1900",
    )
    _assert(
        len(b5.description) > 5,
        "S2/bbb5: empty description did not overwrite real one",
    )
    _assert_eq(
        b5.metadata.contact_name,
        "Claire Contact",
        "S2/bbb5: empty contact_name blocked",
    )
    _assert_eq(
        b5.metadata.contact_phone,
        "514-555-0005",
        "S2/bbb5: empty contact_phone blocked",
    )
    _assert(
        len(b5.metadata.photo_urls) > 0,
        "S2/bbb5: empty photo list blocked",
    )
    _assert_gte(b5.times_seen, 4, "S2/bbb5: times_seen still incremented")

    # ── bbb6: monotonic boolean protection ───────────────────────
    log.info("\n  ── bbb6: monotonic boolean truth ──")
    b6 = store.listings["bbb6"]
    _assert_eq(b6.amenities.dishwasher, True, "S2/bbb6: dishwasher stays True")
    _assert_eq(b6.amenities.gym, True, "S2/bbb6: gym stays True")
    _assert_eq(b6.amenities.pool, True, "S2/bbb6: pool stays True")
    _assert_eq(b6.amenities.elevator, True, "S2/bbb6: elevator stays True")
    _assert_eq(b6.features.balcony, True, "S2/bbb6: balcony stays True")
    _assert_eq(
        b6.features.air_conditioning, True, "S2/bbb6: A/C stays True"
    )
    _assert_eq(
        b6.features.pets_allowed, True, "S2/bbb6: pets_allowed stays True"
    )

    # ── bbb7: new listing fully inserted ─────────────────────────
    log.info("\n  ── bbb7: new listing inserted ──")
    _assert_in("bbb7", store.listings, "S2/bbb7: present in store")
    b7 = store.listings["bbb7"]
    _assert_eq(b7.price.base_rent, 2200.0, "S2/bbb7: rent correct")
    _assert_eq(b7.features.bedrooms, 3, "S2/bbb7: beds correct")

    # ── bbb8: quality gate ───────────────────────────────────────
    log.info("\n  ── bbb8: quality gate ──")
    _assert(
        "bbb8" not in store.listings,
        "S2/bbb8: quality-gated listing not in store",
    )


# ════════════════════════════════════════════════════════════════════
#  SCENARIO 3 — Cross-domain dedup, normalisation edge cases,
#               and adversarial scraper output
# ════════════════════════════════════════════════════════════════════

def scenario_3():
    log.info("\n" + "=" * 70)
    log.info("SCENARIO 3 — Normalisation, cross-domain, adversarial inputs")
    log.info("=" * 70)

    pre_store = [
        # ccc1: has a real adjusted_rent — scraper returns 0 for it
        _listing(
            lid="ccc1",
            full_address="10 Atwater, Montreal",
            city="Montreal",
            base_rent=1800,
            adjusted_rent=1950.0,
            parking_fee=150.0,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # ccc2: city written differently in previous run
        _listing(
            lid="ccc2",
            full_address="20 Boul. des Laurentides, Laval",
            city="laval",
            province="quebec",
            base_rent=1500,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # ccc3: was a studio, must stay studio after normalisation
        _listing(
            lid="ccc3",
            full_address="30 Plateau, Montreal",
            city="Montreal",
            base_rent=1100,
            bedrooms=0,
            bathrooms=1.0,
            property_type=PropertyType.STUDIO,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=1,
        ),
        # ccc4: has parking_fee — scraper returns adjusted_rent=0
        _listing(
            lid="ccc4",
            full_address="40 Verdun, Verdun",
            city="Verdun",
            base_rent=1600,
            adjusted_rent=1700.0,
            parking_fee=100.0,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T0,
            last_seen=_T0,
            times_seen=2,
        ),
    ]

    dump = [
        # ccc1: adjusted_rent=0 and parking_fee=None — protected
        _listing(
            lid="ccc1",
            full_address="10 Atwater, Montreal",
            city="Montreal",
            base_rent=1800,
            adjusted_rent=0,
            parking_fee=None,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc2: city now "Laval" (proper casing) — normaliser
        # should unify both to "Laval"
        _listing(
            lid="ccc2",
            full_address="20 Boul. des Laurentides, Laval",
            city="Laval",
            province="QC",
            base_rent=1550,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc3: 0 bedrooms — normaliser turns apartment → studio
        _listing(
            lid="ccc3",
            full_address="30 Plateau, Montreal",
            city="Montreal",
            base_rent=1100,
            bedrooms=0,
            bathrooms=1.0,
            property_type=PropertyType.APARTMENT,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc4: adjusted_rent=0 — must not wipe 1700
        _listing(
            lid="ccc4",
            full_address="40 Verdun, Verdun",
            city="Verdun",
            base_rent=1600,
            adjusted_rent=0,
            parking_fee=100.0,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc5: negative rent (scraper bug) — normaliser fixes
        _listing(
            lid="ccc5",
            full_address="50 Villeray, Montreal",
            city="montréal",
            province="québec",
            base_rent=-1400,
            bedrooms=2,
            bathrooms=1.0,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc6 + ccc6_dup: same ID, different richness — dedup
        _listing(
            lid="ccc6",
            full_address="60 NDG, Montreal",
            city="Montreal",
            base_rent=1350,
            bedrooms=1,
            bathrooms=1.0,
            first_seen=_T1,
            last_seen=_T1,
        ),
        _listing(
            lid="ccc6",
            full_address="60 NDG, Montreal",
            city="Montreal",
            base_rent=1350,
            bedrooms=1,
            bathrooms=1.0,
            description="Detailed description with hardwood floors.",
            photo_urls=["https://example.com/ccc6.jpg"],
            dishwasher=True,
            first_seen=_T1,
            last_seen=_T1,
        ),
        # ccc7: quality gate — has address but nothing else
        _listing(
            lid="ccc7",
            full_address="70 Nowhere, Unknown",
            city="Unknown",
            base_rent=0,
            bedrooms=None,
            bathrooms=None,
            sqft=None,
            source_url="",
            first_seen=_T1,
            last_seen=_T1,
        ),
    ]

    store, report = _run_pipeline(dump, pre_store)

    # ── Report ───────────────────────────────────────────────────
    log.info("\n  ── Report ──")
    _assert(report.added >= 2, "S3: ccc5, ccc6 added")
    _assert(report.skipped_quality >= 1, "S3: ccc7 quality-gated")

    # ── ccc1: adjusted_rent zero-protection ──────────────────────
    log.info("\n  ── ccc1: adjusted_rent + parking_fee protection ──")
    c1 = store.listings["ccc1"]
    _assert_eq(
        c1.price.adjusted_rent,
        1950.0,
        "S3/ccc1: adjusted_rent=0 blocked",
    )
    _assert_eq(
        c1.price.parking_fee,
        150.0,
        "S3/ccc1: parking_fee=None blocked",
    )

    # ── ccc2: city normalisation ─────────────────────────────────
    log.info("\n  ── ccc2: city + province normalisation ──")
    c2 = store.listings["ccc2"]
    _assert_eq(c2.address.city, "Laval", "S3/ccc2: city normalised to Laval")
    _assert_eq(c2.address.province, "QC", "S3/ccc2: province normalised to QC")
    _assert_eq(
        c2.price.base_rent, 1550.0, "S3/ccc2: price updated 1500 → 1550"
    )

    # ── ccc3: studio normalisation ───────────────────────────────
    log.info("\n  ── ccc3: bedroom=0 → studio ──")
    c3 = store.listings["ccc3"]
    _assert_eq(
        c3.features.property_type,
        PropertyType.STUDIO,
        "S3/ccc3: 0-bed normalised to STUDIO",
    )

    # ── ccc4: adjusted_rent zero-protection ──────────────────────
    log.info("\n  ── ccc4: adjusted_rent zero-protection ──")
    c4 = store.listings["ccc4"]
    _assert_eq(
        c4.price.adjusted_rent,
        1700.0,
        "S3/ccc4: adjusted_rent=0 did not wipe 1700",
    )

    # ── ccc5: negative rent fixed by normaliser ──────────────────
    log.info("\n  ── ccc5: negative rent normalised ──")
    _assert_in("ccc5", store.listings, "S3/ccc5: inserted")
    c5 = store.listings["ccc5"]
    _assert_eq(c5.price.base_rent, 1400.0, "S3/ccc5: rent = abs(-1400)")
    _assert_eq(c5.address.city, "Montreal", "S3/ccc5: montréal → Montreal")
    _assert_eq(c5.address.province, "QC", "S3/ccc5: québec → QC")

    # ── ccc6: intra-batch dedup keeps richer copy ────────────────
    log.info("\n  ── ccc6: dedup keeps richer copy ──")
    _assert_in("ccc6", store.listings, "S3/ccc6: present")
    c6 = store.listings["ccc6"]
    _assert(
        len(c6.description) > 10,
        "S3/ccc6: richer copy kept (has description)",
    )
    _assert(
        len(c6.metadata.photo_urls) > 0,
        "S3/ccc6: richer copy kept (has photos)",
    )
    _assert_eq(c6.amenities.dishwasher, True, "S3/ccc6: amenity from richer copy")

    # ── ccc7: quality gate ───────────────────────────────────────
    log.info("\n  ── ccc7: quality gate ──")
    _assert(
        "ccc7" not in store.listings,
        "S3/ccc7: missing critical fields → rejected",
    )


# ════════════════════════════════════════════════════════════════════
#  SCENARIO 4 — Trivial / boundary cases
# ════════════════════════════════════════════════════════════════════

def scenario_4():
    log.info("\n" + "=" * 70)
    log.info("SCENARIO 4 — Trivial and boundary cases")
    log.info("=" * 70)

    # ── 4a: empty scrape into empty store ────────────────────────
    log.info("\n  ── 4a: empty dump + empty store ──")
    store_a, report_a = _run_pipeline([], [])
    _assert_eq(len(store_a.listings), 0, "S4a: store empty")
    _assert_eq(report_a.added, 0, "S4a: nothing added")

    # ── 4b: empty scrape into populated store ────────────────────
    log.info("\n  ── 4b: empty dump + populated store ──")
    pre_b = [
        _listing(lid="ddd1", base_rent=1500, bedrooms=2, bathrooms=1.0),
    ]
    store_b, report_b = _run_pipeline([], pre_b)
    _assert_eq(len(store_b.listings), 1, "S4b: store untouched")
    _assert_eq(report_b.added, 0, "S4b: nothing added")

    # ── 4c: identical re-scrape — zero changes ──────────────────
    log.info("\n  ── 4c: identical re-scrape ──")
    listing_c = _listing(
        lid="eee1",
        full_address="1000 Guy, Montreal",
        city="Montreal",
        base_rent=1600,
        bedrooms=2,
        bathrooms=1.0,
        sqft=800,
        first_seen=_T0,
        last_seen=_T0,
        times_seen=1,
    )
    pre_c = [copy.deepcopy(listing_c)]
    dump_c = [copy.deepcopy(listing_c)]
    store_c, report_c = _run_pipeline(dump_c, pre_c)
    _assert_eq(report_c.added, 0, "S4c: none added")
    _assert_eq(report_c.updated, 0, "S4c: none updated (fields identical)")

    eee1 = store_c.listings["eee1"]
    _assert_gte(eee1.times_seen, 2, "S4c: times_seen incremented")

    # ── 4d: listing with exactly 3 critical fields passes gate ───
    log.info("\n  ── 4d: quality gate boundary (exactly 3 fields) ──")
    dump_d = [
        _listing(
            lid="fff1",
            full_address="Minimum viable listing",
            base_rent=999,
            bedrooms=None,
            bathrooms=None,
            sqft=None,
            source_url="https://rentals.ca/fff1",
        ),
    ]
    store_d, report_d = _run_pipeline(dump_d, [])
    _assert_in("fff1", store_d.listings, "S4d: 3-field listing accepted")

    # ── 4e: listing with exactly 2 critical fields fails gate ────
    log.info("\n  ── 4e: quality gate boundary (only 2 fields) ──")
    pre_e = [
        _listing(
            lid="ggg1",
            full_address="Existing record",
            base_rent=1200,
            bedrooms=2,
            bathrooms=1.0,
        ),
    ]
    dump_e = [
        _listing(
            lid="ggg1",
            full_address="Existing record",
            base_rent=0,
            bedrooms=None,
            bathrooms=None,
            sqft=None,
            source_url="https://rentals.ca/ggg1",
        ),
    ]
    store_e, report_e = _run_pipeline(dump_e, pre_e)
    _assert_eq(report_e.skipped_quality, 1, "S4e: 2-field listing quality-gated")
    ggg1 = store_e.listings["ggg1"]
    _assert_eq(
        ggg1.price.base_rent,
        1200.0,
        "S4e: original rent preserved after quality gate",
    )
    _assert_gte(ggg1.times_seen, 2, "S4e: times_seen still incremented")

    # ── 4f: all-duplicate scrape batch ───────────────────────────
    log.info("\n  ── 4f: all-duplicate batch deduplicates to 1 ──")
    dup = _listing(
        lid="hhh1",
        base_rent=1000,
        bedrooms=1,
        bathrooms=1.0,
    )
    dump_f = [copy.deepcopy(dup) for _ in range(5)]
    store_f, report_f = _run_pipeline(dump_f, [])
    _assert_eq(len(store_f.listings), 1, "S4f: 5 dupes → 1 listing")
    _assert_eq(report_f.added, 1, "S4f: added exactly 1")


# ════════════════════════════════════════════════════════════════════
#  Runner
# ════════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 70)
    log.info("STORE MERGE TEST SUITE")
    log.info("=" * 70)

    scenario_1()
    scenario_2()
    scenario_3()
    scenario_4()

    log.info("\n" + "=" * 70)
    if _failures:
        log.error(f"FINISHED WITH {len(_failures)} FAILURE(S):")
        for f in _failures:
            log.error(f"  • {f}")
        sys.exit(1)
    else:
        log.info("ALL ASSERTIONS PASSED ✓")
        sys.exit(0)


if __name__ == "__main__":
    main()