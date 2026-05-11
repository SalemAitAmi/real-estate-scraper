"""
Unified data models for rental listings across all sites.
"""

from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
import hashlib
import copy


# ────────────────────────────────────────────────────────────────────
#  Enumerations
# ────────────────────────────────────────────────────────────────────

class PropertyType(Enum):
    APARTMENT = "apartment"
    CONDO = "condo"
    HOUSE = "house"
    TOWNHOUSE = "townhouse"
    DUPLEX = "duplex"
    TRIPLEX = "triplex"
    STUDIO = "studio"
    LOFT = "loft"
    BASEMENT = "basement"
    ROOM = "room"
    OTHER = "other"


class HeatingType(Enum):
    ELECTRIC = "electric"
    GAS = "gas"
    OIL = "oil"
    HYDRONIC = "hydronic"
    RADIANT = "radiant"
    FORCED_AIR = "forced_air"
    HEAT_PUMP = "heat_pump"
    BASEBOARD = "baseboard"
    CENTRAL = "central"
    UNKNOWN = "unknown"


class ParkingType(Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    UNDERGROUND = "underground"
    GARAGE = "garage"
    STREET = "street"
    NONE = "none"


class LaundryType(Enum):
    IN_UNIT = "in_unit"
    IN_BUILDING = "in_building"
    HOOKUPS = "hookups"
    NONE = "none"


# ────────────────────────────────────────────────────────────────────
#  Nested value objects
# ────────────────────────────────────────────────────────────────────

@dataclass
class Address:
    full_address: str
    street_number: Optional[str] = None
    street_name: Optional[str] = None
    unit_number: Optional[str] = None
    city: str = ""
    province: str = ""
    postal_code: Optional[str] = None
    country: str = "Canada"
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def __str__(self) -> str:
        return self.full_address


@dataclass
class PriceInfo:
    base_rent: float
    currency: str = "CAD"
    adjusted_rent: Optional[float] = None
    heating_included: bool = False
    electricity_included: bool = False
    water_included: bool = False
    internet_included: bool = False
    parking_fee: Optional[float] = None
    storage_fee: Optional[float] = None
    pet_fee: Optional[float] = None
    security_deposit: Optional[float] = None
    first_last_required: bool = False

    def calculate_adjusted_rent(self) -> float:
        total = self.base_rent
        if self.parking_fee:
            total += self.parking_fee
        if self.storage_fee:
            total += self.storage_fee
        return total


@dataclass
class PropertyFeatures:
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    square_feet: Optional[int] = None
    square_meters: Optional[int] = None
    property_type: PropertyType = PropertyType.APARTMENT
    floor_level: Optional[int] = None
    total_floors: Optional[int] = None
    year_built: Optional[int] = None
    heating_type: HeatingType = HeatingType.UNKNOWN
    air_conditioning: bool = False
    parking_type: Optional[ParkingType] = None
    parking_spots: int = 0
    storage_locker: bool = False
    laundry: LaundryType = LaundryType.NONE
    balcony: bool = False
    patio: bool = False
    backyard: bool = False
    pets_allowed: bool = False
    cats_allowed: Optional[bool] = None
    dogs_allowed: Optional[bool] = None
    pet_restrictions: Optional[str] = None
    furnished: bool = False
    style: Optional[str] = None

    def get_sqft(self) -> Optional[int]:
        if self.square_feet:
            return self.square_feet
        if self.square_meters:
            return int(self.square_meters * 10.764)
        return None


@dataclass
class Amenities:
    dishwasher: bool = False
    refrigerator: bool = True
    stove: bool = True
    microwave: bool = False
    garbage_disposal: bool = False
    gym: bool = False
    pool: bool = False
    concierge: bool = False
    rooftop: bool = False
    elevator: bool = False
    security_system: bool = False
    intercom: bool = False
    key_fob: bool = False
    wheelchair_accessible: bool = False
    other_amenities: List[str] = field(default_factory=list)


@dataclass
class ListingMetadata:
    source_site: str
    source_url: str
    source_id: str
    scraped_at: datetime = field(default_factory=datetime.now)
    posted_date: Optional[datetime] = None
    last_updated: Optional[datetime] = None
    available_date: Optional[datetime] = None
    lease_term_months: Optional[int] = None
    lease_type: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    is_active: bool = True
    photo_urls: List[str] = field(default_factory=list)
    video_url: Optional[str] = None
    virtual_tour_url: Optional[str] = None
    price_change: Optional[str] = None
    time_on_site: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
#  Serialisation helpers
# ────────────────────────────────────────────────────────────────────

def _convert(obj: Any) -> Any:
    """Recursively convert a dataclass tree into plain dicts/lists/values."""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _convert(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, (list, tuple)):
        return [_convert(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _convert(v) for k, v in obj.items()}
    return obj


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        return None


# ────────────────────────────────────────────────────────────────────
#  Main listing model
# ────────────────────────────────────────────────────────────────────

@dataclass
class RentalListing:
    id: str
    address: Address
    price: PriceInfo
    features: PropertyFeatures
    amenities: Amenities
    metadata: ListingMetadata
    title: str = ""
    description: str = ""

    is_selected: bool = False
    is_discarded: bool = False
    email_thread_id: Optional[str] = None
    has_unread_email: bool = False
    user_notes: str = ""

    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    times_seen: int = 1

    neighbourhood: Optional[str] = None
    amenities_nearby: Optional[str] = None
    utilities_sewer: Optional[str] = None

    # ── ID generation ──

    @staticmethod
    def generate_id(source_site: str, source_id: str, unique_key: str) -> str:
        """Generate a unique, stable ID for the listing.

        Args:
            source_site: Domain name (e.g. 'realtor.ca').
            source_id:   Site-specific listing identifier.
            unique_key:  Additional distinguishing value — use the full
                        listing URL so that different units at the same
                        street address always produce distinct IDs.
        """
        raw = f"{source_site}:{source_id}:{unique_key}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── Serialisation ──

    def to_dict(self) -> Dict[str, Any]:
        return _convert(self)

    def to_excel_row(self) -> Dict[str, Any]:
        return {
            "ID": self.id,
            "Source": self.metadata.source_site,
            "Title": self.title,
            "Address": str(self.address),
            "City": self.address.city,
            "Price": self.price.base_rent,
            "Adj. Rent": self.price.adjusted_rent or self.price.base_rent,
            "Beds": self.features.bedrooms,
            "Baths": self.features.bathrooms,
            "Sq.Ft.": self.features.get_sqft(),
            "Type": self.features.property_type.value,
            "Heating": self.features.heating_type.value,
            "Heat Incl.": self.price.heating_included,
            "A/C": self.features.air_conditioning,
            "Laundry": self.features.laundry.value,
            "Parking": (
                self.features.parking_type.value
                if self.features.parking_type else "N/A"
            ),
            "Pets": self.features.pets_allowed,
            "Balcony": self.features.balcony,
            "Gym": self.amenities.gym,
            "Posted": self.metadata.posted_date,
            "Available": self.metadata.available_date,
            "URL": self.metadata.source_url,
            "Email": self.email_thread_id or "",
            "Unread": self.has_unread_email,
            "Notes": self.user_notes,
            "First Seen": self.first_seen,
            "Last Seen": self.last_seen,
        }

    # ── Deserialisation ──

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RentalListing":
        # --- Address ---
        address = Address(**data["address"])

        # --- Price ---
        price = PriceInfo(**data["price"])

        # --- Features (with enum reconstruction) ---
        fd = data["features"].copy()
        fd["property_type"] = PropertyType(fd.get("property_type", "apartment"))
        fd["heating_type"] = HeatingType(fd.get("heating_type", "unknown"))
        if fd.get("parking_type"):
            fd["parking_type"] = ParkingType(fd["parking_type"])
        fd["laundry"] = LaundryType(fd.get("laundry", "none"))
        features = PropertyFeatures(**fd)

        # --- Amenities ---
        amenities = Amenities(**data["amenities"])

        # --- Metadata (datetime parsing) ---
        md = data["metadata"].copy()
        for key in ("scraped_at", "posted_date", "last_updated", "available_date"):
            md[key] = _parse_dt(md.get(key))
        metadata = ListingMetadata(**md)

        return cls(
            id=data["id"],
            address=address,
            price=price,
            features=features,
            amenities=amenities,
            metadata=metadata,
            title=data.get("title", ""),
            description=data.get("description", ""),
            is_selected=data.get("is_selected", False),
            is_discarded=data.get("is_discarded", False),
            email_thread_id=data.get("email_thread_id"),
            has_unread_email=data.get("has_unread_email", False),
            user_notes=data.get("user_notes", ""),
            first_seen=_parse_dt(data.get("first_seen")) or datetime.now(),
            last_seen=_parse_dt(data.get("last_seen")) or datetime.now(),
            times_seen=data.get("times_seen", 1),
            neighbourhood=data.get("neighbourhood"),
            amenities_nearby=data.get("amenities_nearby"),
            utilities_sewer=data.get("utilities_sewer"),
        )


# ────────────────────────────────────────────────────────────────────
#  Building → expanded listings
# ────────────────────────────────────────────────────────────────────

@dataclass
class BuildingListing:
    building_id: str
    building_name: Optional[str]
    address: Address
    unit_types: List[Dict[str, Any]] = field(default_factory=list)
    metadata: ListingMetadata = field(
        default_factory=lambda: ListingMetadata(
            source_site="", source_url="", source_id=""
        )
    )
    amenities: Amenities = field(default_factory=Amenities)

    def expand_to_listings(
        self, target_bedrooms: Optional[int] = None
    ) -> List[RentalListing]:
        listings: List[RentalListing] = []
        for ut in self.unit_types:
            if target_bedrooms is not None and ut.get("bedrooms") != target_bedrooms:
                continue
            lid = RentalListing.generate_id(
                self.metadata.source_site,
                f"{self.building_id}_{ut.get('bedrooms', 0)}br",
                str(self.address),
            )
            listings.append(
                RentalListing(
                    id=lid,
                    address=copy.deepcopy(self.address),
                    price=PriceInfo(base_rent=ut.get("price_min", 0)),
                    features=PropertyFeatures(
                        bedrooms=ut.get("bedrooms"),
                        bathrooms=ut.get("bathrooms"),
                        square_feet=ut.get("sqft"),
                    ),
                    amenities=copy.deepcopy(self.amenities),
                    metadata=copy.deepcopy(self.metadata),
                    title=(
                        f"{self.building_name or self.address.full_address}"
                        f" – {ut.get('bedrooms', 0)} BR"
                    ),
                )
            )
        return listings