"""
Gmail API client (skeleton).

Implementation notes for future work
────────────────────────────────────
• Auth: OAuth 2.0 installed-app flow via google-auth-oauthlib.
  Credentials cached under ./config/gmail_token.json.
  Scopes required:
      https://www.googleapis.com/auth/gmail.compose
      https://www.googleapis.com/auth/gmail.readonly
• Service handle: googleapiclient.discovery.build("gmail", "v1", ...)
• Draft creation:  users().drafts().create(userId="me", body={...})
• Thread fetch:    users().threads().get(userId="me", id=thread_id,
                                          format="metadata")
• Unread flag:     any message in thread has labelIds containing "UNREAD"
• Web URL pattern: https://mail.google.com/mail/u/0/#inbox/<thread_id>

This file establishes the interface contract; replace the stub bodies
when the API client is actually wired in.
"""

import base64
import logging
from email.mime.text import MIMEText
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

    def __init__(
        self,
        credentials_path: Path = Path("./config/gmail_credentials.json"),
        token_path:       Path = Path("./config/gmail_token.json"),
    ):
        self.credentials_path = credentials_path
        self.token_path       = token_path
        self._service = None  # lazy-built

    # ── Lazy auth ────────────────────────────────────────────────

    def _get_service(self):
        """Build (and cache) the Gmail API service handle.

        TODO: implement OAuth flow.  Outline:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            ...
        """
        if self._service is None:
            raise NotImplementedError(
                "GmailClient auth not yet implemented — see module docstring."
            )
        return self._service

    # ── EmailClient interface ────────────────────────────────────

    def create_draft(self, req: DraftRequest) -> ThreadRef:
        """Create a Gmail draft, return its thread ref.

        TODO: build MIME message, base64-url encode, POST to
              users().drafts().create().  Response includes
              message.threadId — wrap it in ThreadRef.
        """
        raise NotImplementedError

    def get_thread(self, thread_id: str) -> Optional[ThreadRef]:
        """Fetch a single thread's metadata.

        TODO: users().threads().get(...).execute(); inspect labelIds
              of each message for "UNREAD" to set has_unread.
        """
        raise NotImplementedError

    def sync_threads(self, thread_ids: List[str]) -> List[ThreadRef]:
        """Refresh many threads at once.

        TODO: batch via googleapiclient BatchHttpRequest; fall back
              to sequential get_thread calls until then.
        """
        raise NotImplementedError

    def web_url_for(self, thread_id: str) -> str:
        return GMAIL_WEB_THREAD.format(tid=thread_id)


# ── Stub fallback so the rest of the app runs before Gmail is wired ──

class NullEmailClient(EmailClient):
    """No-op client used until Gmail auth is implemented.

    Records intent locally so the 📧 indicator still works, and
    surfaces a generic Gmail search URL as the 'open thread' link.
    """

    def create_draft(self, req: DraftRequest) -> ThreadRef:
        synthetic = f"local_{req.listing_id[:8]}"
        logger.info(
            f"NullEmailClient draft (not sent): "
            f"to={req.to!r} subject={req.subject!r}"
        )
        return ThreadRef(
            thread_id=synthetic,
            web_url=f"https://mail.google.com/mail/u/0/#search/{req.subject}",
        )

    def get_thread(self, thread_id: str) -> Optional[ThreadRef]:
        return None

    def sync_threads(self, thread_ids: List[str]) -> List[ThreadRef]:
        return []

    def web_url_for(self, thread_id: str) -> str:
        return "https://mail.google.com/mail/u/0/#inbox"