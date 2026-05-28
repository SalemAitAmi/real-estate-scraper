"""
Persistent mapping: listing_id → thread_id.

Kept separate from ListingStore because email state is provider-
specific bookkeeping, not part of the canonical listing record.
The store still owns the *display* flags (email_thread_id,
has_unread_email) via set_email_thread().
"""

import json
from pathlib import Path
from typing import Dict, Optional


class ThreadIndex:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path("./data/threads.json")
        self._map: Dict[str, str] = {}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self._map = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._map = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._map, indent=2), encoding="utf-8"
        )

    def get(self, listing_id: str) -> Optional[str]:
        return self._map.get(listing_id)

    def set(self, listing_id: str, thread_id: str):
        self._map[listing_id] = thread_id
        self.save()

    def all_thread_ids(self) -> list:
        return list(self._map.values())

    def listing_for_thread(self, thread_id: str) -> Optional[str]:
        for lid, tid in self._map.items():
            if tid == thread_id:
                return lid
        return None