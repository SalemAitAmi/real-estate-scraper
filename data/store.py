"""
Persistent JSON store with smart merge logic.

Design principles
─────────────────
• A listing's identity is its ``id`` (SHA-256 of source_site + source_id + address).
• User actions (select / discard / notes / email threads) are NEVER overwritten.
• Boolean amenity flags use *monotonic truth*: once True, a merge will not
  reset them to False (protects against scraper pages that simply lack the info).
• A quality gate prevents a bad scrape (mostly null) from corrupting an
  existing record.
"""

import json
import logging
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import RentalListing

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
#  Merge result
# ────────────────────────────────────────────────────────────────────

@dataclass
class MergeReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_quality: int = 0
    field_changes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Added {self.added} · Updated {self.updated} "
            f"· Unchanged {self.unchanged} · Skipped(quality) {self.skipped_quality}"
        )


# ────────────────────────────────────────────────────────────────────
#  Store
# ────────────────────────────────────────────────────────────────────

class ListingStore:
    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = store_path or Path("./data/store.json")
        self.listings: Dict[str, RentalListing] = {}
        self.load()

    # ── Persistence ────────────────────────────────────────────────

    def load(self):
        if not self.store_path.exists():
            return
        try:
            from .normalizer import normalize_listing   # local import: avoid cycle
            with open(self.store_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data:
                listing = RentalListing.from_dict(item)
                # Normalise every loaded listing so derived fields
                # (adjusted_rent, canonical city/province, etc.) match
                # what an incoming scrape will produce.  Without this,
                # an identical re-scrape generates phantom "updates"
                # purely from None → computed-default deltas.
                normalize_listing(listing)
                self.listings[listing.id] = listing
            logger.info(f"Loaded {len(self.listings)} listings from store")
        except Exception as exc:
            logger.error(f"Failed to load store: {exc}")

    def save(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = [l.to_dict() for l in self.listings.values()]
        with open(self.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(self.listings)} listings to {self.store_path}")

    # ── Merge ──────────────────────────────────────────────────────

    def merge_results(self, new_listings: List[RentalListing]) -> MergeReport:
        report = MergeReport()
        for new in new_listings:
            existing = self.listings.get(new.id)
            if existing is None:
                # Quality gate also applies to first-time inserts —
                # otherwise garbage stubs pollute the store forever.
                if self._quality_score(new) < 3:
                    report.skipped_quality += 1
                    continue
                self.listings[new.id] = new
                report.added += 1
            else:
                changes = self._smart_merge(existing, new)
                if changes is None:
                    report.skipped_quality += 1
                elif changes:
                    report.updated += 1
                    report.field_changes.extend(changes)
                else:
                    report.unchanged += 1
        return report

    # ── Queries ────────────────────────────────────────────────────

    def get_active(self) -> List[RentalListing]:
        return [
            l for l in self.listings.values()
            if not l.is_selected and not l.is_discarded
        ]

    def by_domain(self, domain: str) -> Dict[str, List[RentalListing]]:
        """Return ``{city: [listings]}`` for one domain (active only)."""
        grouped: Dict[str, List[RentalListing]] = {}
        for l in self.listings.values():
            if l.metadata.source_site != domain:
                continue
            if l.is_selected or l.is_discarded:
                continue
            city = l.address.city or "Unknown"
            grouped.setdefault(city, []).append(l)
        for city in grouped:
            grouped[city].sort(key=lambda x: x.price.base_rent or 9999)
        return grouped

    def selected_grouped(self) -> Dict[str, Dict[str, List[RentalListing]]]:
        """``{domain: {city: [listings]}}`` for selected."""
        return self._group_by_flag("is_selected")

    def discarded_grouped(self) -> Dict[str, Dict[str, List[RentalListing]]]:
        return self._group_by_flag("is_discarded")

    def _group_by_flag(self, flag: str) -> Dict[str, Dict[str, List[RentalListing]]]:
        result: Dict[str, Dict[str, List[RentalListing]]] = {}
        for l in self.listings.values():
            if not getattr(l, flag, False):
                continue
            domain = l.metadata.source_site
            city = l.address.city or "Unknown"
            result.setdefault(domain, {}).setdefault(city, []).append(l)
        return result

    # ── User actions ───────────────────────────────────────────────

    def select_listing(self, lid: str):
        if lid in self.listings:
            self.listings[lid].is_selected = True
            self.listings[lid].is_discarded = False

    def discard_listing(self, lid: str):
        if lid in self.listings:
            self.listings[lid].is_discarded = True
            self.listings[lid].is_selected = False

    def restore_listing(self, lid: str):
        if lid in self.listings:
            self.listings[lid].is_selected = False
            self.listings[lid].is_discarded = False

    def set_email_thread(self, lid: str, thread_id: str, has_unread: bool = False):
        if lid in self.listings:
            self.listings[lid].email_thread_id = thread_id
            self.listings[lid].has_unread_email = has_unread

    def set_notes(self, lid: str, notes: str):
        if lid in self.listings:
            self.listings[lid].user_notes = notes

    # ── Smart merge internals ──────────────────────────────────────

    @staticmethod
    def _quality_score(listing: RentalListing) -> int:
        """Count critical fields present — must reach 3/5 to trust the data."""
        score = 0
        if listing.price.base_rent and listing.price.base_rent > 0:
            score += 1
        if listing.address.full_address and listing.address.full_address.strip():
            score += 1
        if listing.features.bedrooms is not None:
            score += 1
        if listing.features.bathrooms is not None:
            score += 1
        if listing.metadata.source_url and listing.metadata.source_url.strip():
            score += 1
        return score

    def _smart_merge(
        self, existing: RentalListing, new: RentalListing
    ) -> Optional[List[str]]:
        """
        Update *existing* in place.  Returns list of changed field paths,
        empty list if nothing changed, or ``None`` if quality gate failed.
        """
        if self._quality_score(new) < 3:
            existing.last_seen = datetime.now()
            existing.times_seen += 1
            return None

        changes: List[str] = []

        # Merge nested dataclass objects
        changes += self._merge_dc(existing.address, new.address, "address")
        changes += self._merge_dc(
            existing.price, new.price, "price",
            protect_zero={"base_rent", "adjusted_rent"},
        )
        changes += self._merge_dc(existing.features, new.features, "features")
        changes += self._merge_dc(existing.amenities, new.amenities, "amenities")
        changes += self._merge_metadata(existing.metadata, new.metadata)

        # Top-level text fields
        for attr in ("title", "description", "neighbourhood", "amenities_nearby",
                      "utilities_sewer"):
            changes += self._merge_scalar(existing, new, attr)

        # Tracking
        existing.last_seen = datetime.now()
        existing.times_seen += 1

        return changes

    # ---- field-level helpers ----

    def _merge_dc(
        self, old_obj, new_obj, prefix: str,
        protect_zero: set = None,
    ) -> List[str]:
        protect_zero = protect_zero or set()
        changes: List[str] = []
        for f in dc_fields(old_obj):
            name = f.name
            old_v = getattr(old_obj, name)
            new_v = getattr(new_obj, name)
            if name in protect_zero and isinstance(new_v, (int, float)) and new_v == 0:
                continue
            if self._should_update(old_v, new_v):
                setattr(old_obj, name, new_v)
                changes.append(f"{prefix}.{name}")
        return changes

    def _merge_metadata(
        self, old_md, new_md
    ) -> List[str]:
        changes: List[str] = []
        # Only update selected metadata fields (preserve scraped_at, etc.)
        for name in ("posted_date", "last_updated", "available_date",
                      "contact_name", "contact_phone", "contact_email",
                      "photo_urls", "price_change", "time_on_site",
                      "lease_term_months", "lease_type"):
            old_v = getattr(old_md, name)
            new_v = getattr(new_md, name)
            if self._should_update(old_v, new_v):
                setattr(old_md, name, new_v)
                changes.append(f"metadata.{name}")
        old_md.last_updated = datetime.now()
        old_md.is_active = new_md.is_active
        return changes

    def _merge_scalar(self, old_obj, new_obj, attr: str) -> List[str]:
        old_v = getattr(old_obj, attr, None)
        new_v = getattr(new_obj, attr, None)
        if self._should_update(old_v, new_v):
            setattr(old_obj, attr, new_v)
            return [attr]
        return []

    @staticmethod
    def _should_update(old_val: Any, new_val: Any) -> bool:
        if new_val is None:
            return False
        if old_val == new_val:
            return False
        # Protect non-empty strings from being wiped
        if isinstance(new_val, str) and not new_val.strip():
            return False
        # Protect non-empty lists from being emptied
        if isinstance(new_val, list) and not new_val and old_val:
            return False
        # Monotonic truth for booleans
        if isinstance(old_val, bool) and isinstance(new_val, bool):
            return new_val and not old_val
        return True