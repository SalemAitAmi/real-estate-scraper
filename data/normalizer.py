"""
Post-scrape normalisation & intra-domain deduplication.
"""

import re, unicodedata
from typing import Dict, List
from .models import RentalListing

_CITY_CANONICAL: Dict[str, str] = {
    "montréal": "Montreal", "montreal": "Montreal", "mtl": "Montreal",
    "laval": "Laval", "longueuil": "Longueuil", "brossard": "Brossard",
    "terrebonne": "Terrebonne", "repentigny": "Repentigny",
    "saint-laurent": "Saint-Laurent", "st-laurent": "Saint-Laurent",
    "saint-léonard": "Saint-Léonard", "st-léonard": "Saint-Léonard",
    "verdun": "Verdun", "lasalle": "LaSalle", "lachine": "Lachine",
    "dorval": "Dorval", "pointe-claire": "Pointe-Claire",
    "côte-saint-luc": "Côte-Saint-Luc", "cote-saint-luc": "Côte-Saint-Luc",
    "westmount": "Westmount", "outremont": "Outremont",
    "dollard-des-ormeaux": "Dollard-Des Ormeaux",
    "boucherville": "Boucherville", "saint-hubert": "Saint-Hubert",
    "châteauguay": "Châteauguay", "chateauguay": "Châteauguay",
    "blainville": "Blainville", "mirabel": "Mirabel",
    "gatineau": "Gatineau", "québec": "Québec", "quebec": "Québec",
}

_PROVINCE_CANONICAL: Dict[str, str] = {
    "quebec": "QC", "québec": "QC", "qc": "QC",
    "ontario": "ON", "on": "ON", "british columbia": "BC", "bc": "BC",
    "alberta": "AB", "ab": "AB", "manitoba": "MB", "mb": "MB",
    "saskatchewan": "SK", "sk": "SK", "nova scotia": "NS", "ns": "NS",
    "new brunswick": "NB", "nb": "NB",
    "newfoundland and labrador": "NL", "nl": "NL",
    "prince edward island": "PE", "pe": "PE",
}


def _strip_accents_lower(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def normalize_city(raw: str) -> str:
    return _CITY_CANONICAL.get(_strip_accents_lower(raw), raw.strip().title())

def normalize_province(raw: str) -> str:
    return _PROVINCE_CANONICAL.get(raw.strip().lower(), raw.strip().upper()[:2])

def normalize_listing(listing: RentalListing) -> RentalListing:
    if listing.address.city:
        listing.address.city = normalize_city(listing.address.city)
    if listing.address.province:
        listing.address.province = normalize_province(listing.address.province)
    if listing.price.base_rent.amount and listing.price.base_rent.amount < 0:
        listing.price.base_rent.amount = abs(listing.price.base_rent.amount)
    if listing.features.bedrooms == 0:
        from .models import PropertyType
        if listing.features.property_type == PropertyType.APARTMENT:
            listing.features.property_type = PropertyType.STUDIO
    listing.description = listing.description.strip()
    return listing


def deduplicate_listings(listings: List[RentalListing]) -> List[RentalListing]:
    by_id: Dict[str, RentalListing] = {}
    for l in listings:
        existing = by_id.get(l.id)
        if existing is None or _richness(l) > _richness(existing):
            by_id[l.id] = l
    return list(by_id.values())


def _richness(listing: RentalListing) -> int:
    score = sum(1 for v in listing.to_excel_row().values()
                if v is not None and v != "" and v != 0 and v is not False)
    if listing.description: score += 2
    score += len(listing.metadata.photo_urls)
    return score