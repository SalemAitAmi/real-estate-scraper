"""
Abstract email-client contract.

The Excel interface depends only on this module.  Swapping providers
(Gmail today, IMAP/SMTP or Graph later) is a matter of writing a new
EmailClient subclass — no callers change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class DraftRequest:
    """A composed message awaiting send/display."""
    to:      str
    subject: str
    body:    str
    listing_id: str           # for thread-index bookkeeping
    cc:  Optional[str] = None
    bcc: Optional[str] = None


@dataclass
class ThreadRef:
    """Lightweight view of a provider thread."""
    thread_id: str
    web_url:   str            # one-click open in browser
    has_unread: bool = False
    last_message_at: Optional[str] = None  # ISO timestamp


class EmailClient(ABC):
    """Provider-agnostic interface used by ExcelInterface."""

    # ── Composition ──────────────────────────────────────────────
    @abstractmethod
    def create_draft(self, req: DraftRequest) -> ThreadRef:
        """Create a draft and return the (new) thread reference."""

    # ── Inbox sync ───────────────────────────────────────────────
    @abstractmethod
    def get_thread(self, thread_id: str) -> Optional[ThreadRef]:
        """Refresh a single thread (None if it no longer exists)."""

    @abstractmethod
    def sync_threads(self, thread_ids: List[str]) -> List[ThreadRef]:
        """Batch refresh — used to update the 🔔 unread indicators."""

    # ── Deep linking ─────────────────────────────────────────────
    @abstractmethod
    def web_url_for(self, thread_id: str) -> str:
        """Browser URL that opens the thread in the provider's webmail."""