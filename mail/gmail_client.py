"""
Gmail API client (skeleton) + NullEmailClient fallback.
NullEmailClient now pre-fills the To field from listing contact data.
"""

import logging
from pathlib import Path
from typing import List, Optional

from .client import EmailClient, DraftRequest, ThreadRef

logger = logging.getLogger(__name__)

GMAIL_WEB_THREAD = "https://mail.google.com/mail/u/0/#inbox/{tid}"


class GmailClient(EmailClient):
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    def __init__(self, credentials_path=Path("./config/gmail_credentials.json"),
                 token_path=Path("./config/gmail_token.json")):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None

    def _get_service(self):
        if self._service is None:
            raise NotImplementedError(
                "GmailClient auth not yet implemented.")
        return self._service

    def create_draft(self, req: DraftRequest) -> ThreadRef:
        raise NotImplementedError
    def get_thread(self, thread_id: str) -> Optional[ThreadRef]:
        raise NotImplementedError
    def sync_threads(self, thread_ids: List[str]) -> List[ThreadRef]:
        raise NotImplementedError
    def web_url_for(self, thread_id: str) -> str:
        return GMAIL_WEB_THREAD.format(tid=thread_id)


class NullEmailClient(EmailClient):
    """No-op client used until Gmail auth is wired in."""

    def create_draft(self, req: DraftRequest) -> ThreadRef:
        synthetic = f"local_{req.listing_id[:8]}"
        logger.info("NullEmailClient draft: to=%r subject=%r",
                     req.to, req.subject)
        return ThreadRef(
            thread_id=synthetic,
            web_url=(f"https://mail.google.com/mail/u/0/#search/{req.subject}"))

    def get_thread(self, thread_id): return None
    def sync_threads(self, thread_ids): return []
    def web_url_for(self, thread_id):
        return "https://mail.google.com/mail/u/0/#inbox"