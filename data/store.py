"""
Persistent JSON store with smart merge logic.
"""

import json, logging
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import RentalListing

logger = logging.getLogger(__name__)


@dataclass
class MergeReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_quality: int = 0
    field_changes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"Added {self.added} · Updated {self.updated} "
                f"· Unchanged {self.unchanged} "
                f"· Skipped(quality) {self.skipped_quality}")


class ListingStore:
    def __init__(self, store_path: Optional[Path] = None):
        if store_path is None:
            store_path = Path(__file__).resolve().parent / "store.json"
        self.store_path = Path(store_path)
        self.listings: Dict[str, RentalListing] = {}
        self.load()

    def load(self):
        if not self.store_path.exists():
            return
        try:
            from .normalizer import normalize_listing
            with open(self.store_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data:
                listing = RentalListing.from_dict(item)
                normalize_listing(listing)
                self.listings[listing.id] = listing
            logger.info("Loaded %d listings from store", len(self.listings))
        except Exception as exc:
            logger.error("Failed to load store: %s", exc)

    def save(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = [l.to_dict() for l in self.listings.values()]
        with open(self.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        logger.info("Saved %d listings to %s", len(self.listings), self.store_path)

    def merge_results(self, new_listings: List[RentalListing]) -> MergeReport:
        report = MergeReport()
        for new in new_listings:
            existing = self.listings.get(new.id)
            if existing is None:
                if self._quality_score(new) < 3:
                    report.skipped_quality += 1; continue
                self.listings[new.id] = new; report.added += 1
            else:
                changes = self._smart_merge(existing, new)
                if changes is None: report.skipped_quality += 1
                elif changes: report.updated += 1; report.field_changes.extend(changes)
                else: report.unchanged += 1
        return report

    # ── Queries ────────────────────────────────────────────────────

    def get_active(self) -> List[RentalListing]:
        return [l for l in self.listings.values()
                if not l.is_selected and not l.is_discarded]

    def by_domain(self, domain: str) -> Dict[str, List[RentalListing]]:
        grouped: Dict[str, List[RentalListing]] = {}
        for l in self.listings.values():
            if l.metadata.source_site != domain: continue
            if l.is_selected or l.is_discarded: continue
            grouped.setdefault(l.address.city or "Unknown", []).append(l)
        for city in grouped:
            grouped[city].sort(key=lambda x: x.price.base_rent.amount or 9999)
        return grouped

    def selected_grouped(self):
        return self._group_by_flag("is_selected")

    def discarded_grouped(self):
        return self._group_by_flag("is_discarded")

    def _group_by_flag(self, flag):
        result: Dict[str, Dict[str, List[RentalListing]]] = {}
        for l in self.listings.values():
            if not getattr(l, flag, False): continue
            d = l.metadata.source_site; c = l.address.city or "Unknown"
            result.setdefault(d, {}).setdefault(c, []).append(l)
        return result

    # ── User actions ───────────────────────────────────────────────

    def select_listing(self, lid):
        if lid in self.listings:
            self.listings[lid].is_selected = True
            self.listings[lid].is_discarded = False

    def discard_listing(self, lid):
        if lid in self.listings:
            self.listings[lid].is_discarded = True
            self.listings[lid].is_selected = False

    def restore_listing(self, lid):
        if lid in self.listings:
            self.listings[lid].is_selected = False
            self.listings[lid].is_discarded = False

    def set_email_thread(self, lid, thread_id, has_unread=False):
        if lid in self.listings:
            self.listings[lid].email_thread_id = thread_id
            self.listings[lid].has_unread_email = has_unread

    def set_notes(self, lid, notes):
        if lid in self.listings:
            self.listings[lid].user_notes = notes

    # ── Smart merge internals ──────────────────────────────────────

    @staticmethod
    def _quality_score(listing):
        score = 0
        if listing.price.base_rent and listing.price.base_rent.amount > 0: score += 1
        if listing.address.full_address and listing.address.full_address.strip(): score += 1
        if listing.features.bedrooms is not None: score += 1
        if listing.features.bathrooms is not None: score += 1
        if listing.metadata.source_url and listing.metadata.source_url.strip(): score += 1
        return score

    def _smart_merge(self, existing, new):
        if self._quality_score(new) < 3:
            existing.last_seen = datetime.now(); existing.times_seen += 1
            return None
        changes: List[str] = []
        changes += self._merge_dc(existing.address, new.address, "address")
        changes += self._merge_dc(existing.price, new.price, "price")
        changes += self._merge_dc(existing.features, new.features, "features")
        changes += self._merge_dc(existing.amenities, new.amenities, "amenities")
        changes += self._merge_metadata(existing.metadata, new.metadata)
        for attr in ("description", "neighbourhood", "amenities_nearby",
                      "utilities_sewer"):
            changes += self._merge_scalar(existing, new, attr)
        existing.last_seen = datetime.now(); existing.times_seen += 1
        return changes

    def _merge_dc(self, old_obj, new_obj, prefix):
        changes = []
        for f in dc_fields(old_obj):
            old_v, new_v = getattr(old_obj, f.name), getattr(new_obj, f.name)
            if self._should_update(old_v, new_v):
                setattr(old_obj, f.name, new_v)
                changes.append(f"{prefix}.{f.name}")
        return changes

    def _merge_metadata(self, old_md, new_md):
        changes = []
        for name in ("posted_date", "last_updated", "available_date",
                      "contact_name", "contact_phone", "contact_email",
                      "photo_urls", "price_change", "time_on_site",
                      "lease_term_months", "lease_type"):
            old_v, new_v = getattr(old_md, name), getattr(new_md, name)
            if self._should_update(old_v, new_v):
                setattr(old_md, name, new_v); changes.append(f"metadata.{name}")
        old_md.last_updated = datetime.now()
        old_md.is_active = new_md.is_active
        return changes

    def _merge_scalar(self, old_obj, new_obj, attr):
        old_v, new_v = getattr(old_obj, attr, None), getattr(new_obj, attr, None)
        if self._should_update(old_v, new_v):
            setattr(old_obj, attr, new_v); return [attr]
        return []

    @staticmethod
    def _should_update(old_val, new_val):
        if new_val is None or old_val == new_val: return False
        from .models import RentValue
        if isinstance(new_val, RentValue) and (new_val.amount or 0) == 0: return False
        if isinstance(new_val, str) and not new_val.strip(): return False
        if isinstance(new_val, list) and not new_val and old_val: return False
        if isinstance(old_val, bool) and isinstance(new_val, bool):
            return new_val and not old_val
        return True