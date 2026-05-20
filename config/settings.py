"""
Global configuration for the rental aggregator.
Every parameter is structured as a flat label→value pair so that the
Excel Config sheet can read/write them directly via xlwings.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import json
from datetime import datetime


# ────────────────────────────────────────────────────────────────────
#  Search parameters  (one row per label in the Config sheet)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SearchParameters:
    """User-configurable search parameters."""
    locations: List[str] = field(
        default_factory=lambda: ["Montreal", "Laval", "Longueuil"]
    )

    min_price: Optional[int] = None
    max_price: Optional[int] = 2500

    min_bedrooms: int = 2
    max_bedrooms: Optional[int] = 2

    min_bathrooms: float = 1.0
    max_bathrooms: Optional[float] = None

    min_sqft: Optional[int] = None
    max_sqft: Optional[int] = None

    property_types: List[str] = field(
        default_factory=lambda: ["apartment", "condo", "loft", "studio"]
    )

    pets_allowed: Optional[bool] = None

    # ── Scraper-behaviour toggles ──
    skip_covered_locations: bool = True
    fetch_details: bool = True
    headless: bool = False
    max_pages: int = 1

    # ── Serialisation helpers ──

    _LABEL_MAP: dict = field(default=None, init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "_LABEL_MAP", {
            "Locations":                "locations",
            "Min Price":                "min_price",
            "Max Price":                "max_price",
            "Min Bedrooms":             "min_bedrooms",
            "Max Bedrooms":             "max_bedrooms",
            "Min Bathrooms":            "min_bathrooms",
            "Max Bathrooms":            "max_bathrooms",
            "Min Sq.Ft.":              "min_sqft",
            "Max Sq.Ft.":              "max_sqft",
            "Property Types":           "property_types",
            "Pets Allowed":             "pets_allowed",
            "Skip Covered Locations":   "skip_covered_locations",
            "Fetch Details":            "fetch_details",
            "Headless":                 "headless",
            "Max Pages":                "max_pages",
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in self.__dict__.items()
            if not k.startswith("_") and v is not None
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchParameters":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    # ── Excel round-trip ──

    def to_excel_rows(self) -> List[tuple]:
        """Return ``[(label, value), …]`` for the Config sheet."""
        rows = []
        for label, attr in self._LABEL_MAP.items():
            val = getattr(self, attr)
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            rows.append((label, val))
        return rows

    @classmethod
    def from_excel_rows(cls, rows: List[tuple]) -> "SearchParameters":
        """Inverse of *to_excel_rows*."""
        dummy = cls()
        kw: Dict[str, Any] = {}
        for label, raw in rows:
            attr = dummy._LABEL_MAP.get(label)
            if attr is None or raw is None:
                continue
            if attr in ("locations", "property_types"):
                kw[attr] = [s.strip() for s in str(raw).split(",") if s.strip()]
            elif attr in ("skip_covered_locations", "fetch_details",
                          "headless", "pets_allowed"):
                kw[attr] = bool(raw)
            elif attr in ("min_price", "max_price", "min_sqft",
                          "max_sqft", "max_pages",
                          "min_bedrooms", "max_bedrooms"):
                kw[attr] = int(raw) if raw else None
            elif attr in ("min_bathrooms", "max_bathrooms"):
                kw[attr] = float(raw) if raw else None
            else:
                kw[attr] = raw
        return cls(**kw)


# ────────────────────────────────────────────────────────────────────
#  Scraper / Excel / Outlook settings
# ────────────────────────────────────────────────────────────────────

@dataclass
class ScraperSettings:
    request_timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 2.0
    rate_limit_rpm: int = 30


@dataclass
class ExcelSettings:
    workbook_name: str = "RentalAggregator.xlsm"
    data_directory: Path = field(default_factory=lambda: Path("./data"))
    config_sheet: str = "Config"
    selected_sheet: str = "Selected"
    discarded_sheet: str = "Discarded"
    max_rows_per_site: int = 500
    date_format: str = "YYYY-MM-DD HH:MM"
    currency_format: str = "$#,##0"


@dataclass
class OutlookSettings:
    default_subject_template: str = "Inquiry about rental at {address}"
    default_body_template: str = (
        "Hello,\n\n"
        "I am interested in the rental listing at {address}.\n\n"
        "Could you please provide more information about:\n"
        "- Availability and move-in date\n"
        "- Lease terms\n"
        "- Any additional fees or deposits\n\n"
        "Thank you for your time.\n\nBest regards"
    )
    email_folder: str = "Rental Search"
    auto_create_folder: bool = True


# ────────────────────────────────────────────────────────────────────
#  Top-level Settings singleton
# ────────────────────────────────────────────────────────────────────

class Settings:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or Path("./config/settings.json")
        self.search = SearchParameters()
        self.scraper = ScraperSettings()
        self.excel = ExcelSettings()
        self.outlook = OutlookSettings()
        self.enabled_sites: List[str] = [
            "realtor.ca", "rentals.ca", "apartments.com"
        ]
        if self.config_path.exists():
            self.load()

    def load(self):
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
            if "search" in data:
                self.search = SearchParameters.from_dict(data["search"])
            if "enabled_sites" in data:
                self.enabled_sites = data["enabled_sites"]
        except Exception as exc:
            print(f"Warning: could not load settings – {exc}")

    def save(self):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(
                {
                    "search": self.search.to_dict(),
                    "enabled_sites": self.enabled_sites,
                },
                f,
                indent=2,
            )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings