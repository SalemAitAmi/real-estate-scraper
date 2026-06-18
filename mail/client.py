"""Abstract email-client contract."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class DraftRequest:
    to: str
    subject: str
    body: str
    listing_id: str
    cc: Optional[str] = None
    bcc: Optional[str] = None


@dataclass
class ThreadRef:
    thread_id: str
    web_url: str
    has_unread: bool = False
    last_message_at: Optional[str] = None


class EmailClient(ABC):
    @abstractmethod
    def create_draft(self, req: DraftRequest) -> ThreadRef: ...
    @abstractmethod
    def get_thread(self, thread_id: str) -> Optional[ThreadRef]: ...
    @abstractmethod
    def sync_threads(self, thread_ids: List[str]) -> List[ThreadRef]: ...
    @abstractmethod
    def web_url_for(self, thread_id: str) -> str: ...