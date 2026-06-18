"""
Persistent mapping: listing_id → thread_id.
Supports *pending* inquiries sent via on-site forms where no thread_id
exists until a reply arrives.  Replies are matched by address.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class ThreadIndex:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path(__file__).resolve().parent.parent / "data" / "threads.json"
        self._map: Dict[str, str] = {}
        self._pending: Dict[str, Dict] = {}
        self.load()

    # ── Persistence ────────────────────────────────────────────────

    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "threads" in raw:
                self._map = raw.get("threads", {})
                self._pending = raw.get("pending", {})
            else:
                # Legacy flat format {listing_id: thread_id}
                self._map = raw if isinstance(raw, dict) else {}
        except Exception:
            self._map = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "threads": self._map,
            "pending": self._pending,
        }, indent=2), encoding="utf-8")

    # ── Confirmed threads ──────────────────────────────────────────

    def get(self, listing_id: str) -> Optional[str]:
        return self._map.get(listing_id)

    def set(self, listing_id: str, thread_id: str):
        self._map[listing_id] = thread_id
        self._pending.pop(listing_id, None)
        self.save()

    def all_thread_ids(self) -> List[str]:
        return list(self._map.values())

    def listing_for_thread(self, thread_id: str) -> Optional[str]:
        for lid, tid in self._map.items():
            if tid == thread_id:
                return lid
        return None

    # ── Pending (on-site form — no thread until reply) ─────────────

    def mark_pending(self, listing_id: str, address: str, source_site: str):
        """Record that an inquiry was sent via the site's contact form."""
        self._pending[listing_id] = {
            "address": address,
            "source_site": source_site,
            "sent_at": datetime.now().isoformat(),
        }
        self.save()

    def is_pending(self, listing_id: str) -> bool:
        return listing_id in self._pending

    def pending_entries(self) -> Dict[str, Dict]:
        return dict(self._pending)

    def match_by_address(self, text: str) -> Optional[str]:
        """Return the listing_id whose pending address appears in *text*."""
        text_lower = text.lower()
        for lid, info in self._pending.items():
            if info["address"].lower() in text_lower:
                return lid
        return None

    def resolve_pending(self, listing_id: str, thread_id: str):
        """Promote a pending entry to a confirmed thread."""
        self._pending.pop(listing_id, None)
        self._map[listing_id] = thread_id
        self.save()