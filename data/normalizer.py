"""
Post-scrape normalisation & intra-domain deduplication.

Each scraper already maps raw data into the RentalListing model;
this module applies a final consistency pass so that the same physical
listing always looks identical regardless of minor scraper variance.
"""

import re
import unicodedata
from typing import Dict, List

from .models import RentalListing


# ────────────────────────────────────────────────────────────────────
#  City-name normalisation
# ────────────────────────────────────────────────────────────────────

_CITY_CANONICAL: Dict[str, str] = {
    "montréal": "Montreal",
    "montreal": "Montreal",
    "mtl": "Montreal",
    "laval": "Laval",
    "longueuil": "Longueuil",
    "brossard": "Brossard",
    "terrebonne": "Terrebonne",
    "repentigny": "Repentigny",
    "saint-laurent": "Saint-Laurent",
    "st-laurent": "Saint-Laurent",
    "saint-léonard": "Saint-Léonard",
    "st-léonard": "Saint-Léonard",
    "verdun": "Verdun",
    "lasalle": "LaSalle",
    "lachine": "Lachine",
    "dorval": "Dorval",
    "pointe-claire": "Pointe-Claire",
    "côte-saint-luc": "Côte-Saint-Luc",
    "cote-saint-luc": "Côte-Saint-Luc",
    "westmount": "Westmount",
    "outremont": "Outremont",
    "dollard-des-ormeaux": "Dollard-Des Ormeaux",
    "boucherville": "Boucherville",
    "saint-hubert": "Saint-Hubert",
    "châteauguay": "Châteauguay",
    "chateauguay": "Châteauguay",
    "blainville": "Blainville",
    "mirabel": "Mirabel",
    "gatineau": "Gatineau",
    "québec": "Québec",
    "quebec": "Québec",
}

_PROVINCE_CANONICAL: Dict[str, str] = {
    "quebec": "QC", "québec": "QC", "qc": "QC",
    "ontario": "ON", "on": "ON",
    "british columbia": "BC", "bc": "BC",
    "alberta": "AB", "ab": "AB",
    "manitoba": "MB", "mb": "MB",
    "saskatchewan": "SK", "sk": "SK",
    "nova scotia": "NS", "ns": "NS",
    "new brunswick": "NB", "nb": "NB",
    "newfoundland and labrador": "NL", "nl": "NL",
    "prince edward island": "PE", "pe": "PE",
}


def _strip_accents_lower(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def normalize_city(raw: str) -> str:
    key = _strip_accents_lower(raw)
    return _CITY_CANONICAL.get(key, raw.strip().title())


def normalize_province(raw: str) -> str:
    key = raw.strip().lower()
    return _PROVINCE_CANONICAL.get(key, raw.strip().upper()[:2])


# ────────────────────────────────────────────────────────────────────
#  Full listing normalisation
# ────────────────────────────────────────────────────────────────────

def normalize_listing(listing: RentalListing) -> RentalListing:
    """Apply all normalisation rules *in place* and return the listing."""

    # City / province
    if listing.address.city:
        listing.address.city = normalize_city(listing.address.city)
    if listing.address.province:
        listing.address.province = normalize_province(listing.address.province)

    # Ensure rent is monthly and positive
    if listing.price.base_rent.amount and listing.price.base_rent.amount < 0:
        listing.price.base_rent.amount = abs(listing.price.base_rent.amount)

    # Bedroom = 0 means studio
    if listing.features.bedrooms == 0:
        from .models import PropertyType
        if listing.features.property_type == PropertyType.APARTMENT:
            listing.features.property_type = PropertyType.STUDIO

    # Strip excess whitespace from text fields
    listing.title = " ".join(listing.title.split())
    listing.description = listing.description.strip()

    return listing


# ────────────────────────────────────────────────────────────────────
#  Intra-domain deduplication
# ────────────────────────────────────────────────────────────────────

def deduplicate_listings(listings: List[RentalListing]) -> List[RentalListing]:
    """
    Remove duplicates **within the same domain**.

    When two listings share the same ``id`` (derived from source_site +
    source_id + address), keep the one with the most populated fields.
    """
    by_id: Dict[str, RentalListing] = {}
    for listing in listings:
        existing = by_id.get(listing.id)
        if existing is None:
            by_id[listing.id] = listing
        else:
            if _richness(listing) > _richness(existing):
                by_id[listing.id] = listing
    return list(by_id.values())


def _richness(listing: RentalListing) -> int:
    """Rough count of non-empty fields — used to pick the 'richer' copy."""
    score = 0
    row = listing.to_excel_row()
    for v in row.values():
        if v is not None and v != "" and v != 0 and v is not False:
            score += 1
    if listing.description:
        score += 2
    if listing.metadata.photo_urls:
        score += len(listing.metadata.photo_urls)
    return score