"""
xlwings-based Excel interface for the Rental Aggregator.

Contract with the store
───────────────────────
• The workbook is a *view*. The JSON store is authoritative.
• User intent flows in via the "Action" column on each visible sheet.
• Mutations go through ListingStore's documented methods only
  (select_listing / discard_listing / restore_listing /
   set_email_thread / set_notes) so that the store's smart-merge
   and monotonic-truth guarantees are not bypassed.
• Refresh order is ALWAYS: process_actions → save → rewrite sheets.
"""

import logging
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import xlwings as xw
from mail import EmailClient, DraftRequest, ThreadIndex
from mail.gmail_client import NullEmailClient

from pathlib import Path

try:
    import win32com.client 
    _HAS_OUTLOOK = True
except ImportError:
    _HAS_OUTLOOK = False

from config.settings import SearchParameters, get_settings
from data.models import RentalListing
from data.store import ListingStore

logger = logging.getLogger(__name__)


# ── Column schema ───────────────────────────────────────────────────
COLUMNS: List[Tuple[str, Optional[str]]] = [
    ("Action",      None),
    ("ID",          "ID"),
    ("Source",      "Source"),
    ("Title",       "Title"),
    ("Address",     "Address"),
    ("City",        "City"),
    ("Price",       "Price"),
    ("Beds",        "Beds"),
    ("Baths",       "Baths"),
    ("Sq.Ft.",      "Sq.Ft."),
    ("Type",        "Type"),
    ("Heating",     "Heating"),
    ("Heat Incl.",  "Heat Incl."),
    ("A/C",         "A/C"),
    ("Laundry",     "Laundry"),
    ("Parking",     "Parking"),
    ("Pets",        "Pets"),
    ("Balcony",     "Balcony"),
    ("Gym",         "Gym"),
    ("Posted",      "Posted"),
    ("Available",   "Available"),
    ("URL",         "URL"),
    ("Email",       "Email"),
    ("Unread",      "Unread"),
    ("Notes",       "Notes"),
    ("First Seen",  "First Seen"),
    ("Last Seen",   "Last Seen"),
]
COL_HEADERS = [c[0] for c in COLUMNS]
NUM_COLS    = len(COL_HEADERS)
COL_ACTION  = 1
COL_ID      = 2
COL_URL     = COL_HEADERS.index("URL") + 1
COL_EMAIL   = COL_HEADERS.index("Email") + 1
COL_NOTES   = COL_HEADERS.index("Notes") + 1

# Action vocabulary
ACTIONS_ACTIVE   = "select,discard,message"     # domain sheets
ACTIONS_SELECTED = "discard,message,restore"
ACTIONS_DISCARDED = "select,restore"

# Colours
CLR_HEADER     = (44,  62,  80)
CLR_DOMAIN_BAN = (39, 174,  96)
CLR_CITY_BAN   = (41, 128, 185)
CLR_SELECTED   = (39, 174,  96)
CLR_DISCARDED  = (192,  57,  43)
CLR_WHITE      = (255, 255, 255)



class ExcelInterface:
    def __init__(
        self,
        workbook: Optional["xw.Book"] = None,
        workbook_path: Optional[str] = None,
        store: Optional[ListingStore] = None,
        email_client: Optional[EmailClient] = None,
        thread_index: Optional[ThreadIndex] = None,
    ):
        if workbook is not None:
            self.wb = workbook
        elif workbook_path:
            self.wb = xw.Book(workbook_path)
        else:
            self.wb = xw.Book.caller()
        self.store = store or ListingStore()
        self.email = email_client or NullEmailClient()
        self.threads = thread_index or ThreadIndex()

    # ════════════════════════════════════════════════════════════════
    #  Public entry points
    # ════════════════════════════════════════════════════════════════

    def refresh_all(self, domains: List[str], params: Optional[SearchParameters] = None):
        """Full pipeline: harvest user intent, then rewrite every sheet."""
        # 1) Read Config from the sheet (user edits win) and persist it.
        try:
            self._sync_config_in(params)
        except Exception as exc:
            logger.warning(f"Config read skipped: {exc}")

        # 2) Process any pending actions on visible sheets BEFORE redrawing.
        self.process_actions(domains)

        # 3) Redraw everything from the store.
        self.write_config(get_settings().search)
        self.write_all_domain_sheets(domains)
        self.write_selected_sheet()
        self.write_discarded_sheet()

    # ════════════════════════════════════════════════════════════════
    #  Config
    # ════════════════════════════════════════════════════════════════

    def write_config(self, params: Optional[SearchParameters] = None):
        params = params or get_settings().search
        sht = self._sheet("Config")
        sht.clear()
        sht.range("A1").value = "Parameter"
        sht.range("B1").value = "Value"
        hdr = sht.range("A1:B1")
        hdr.font.bold = True
        hdr.color = CLR_HEADER
        hdr.font.color = CLR_WHITE
        for i, (label, val) in enumerate(params.to_excel_rows(), start=2):
            sht.range(f"A{i}").value = label
            sht.range(f"B{i}").value = val
        sht.autofit("c")

    def read_config(self) -> SearchParameters:
        sht = self.wb.sheets["Config"]
        rows: List[tuple] = []
        last = sht.used_range.last_cell.row
        for r in range(2, last + 1):
            label = sht.range(f"A{r}").value
            if not label:
                continue
            rows.append((label, sht.range(f"B{r}").value))
        return SearchParameters.from_excel_rows(rows)

    def _sync_config_in(self, fallback: Optional[SearchParameters]):
        """Pull edits from the Config sheet into settings.json."""
        if "Config" not in [s.name for s in self.wb.sheets]:
            return
        params = self.read_config()
        settings = get_settings()
        settings.search = params
        settings.save()

    # ════════════════════════════════════════════════════════════════
    #  Domain sheets
    # ════════════════════════════════════════════════════════════════

    def write_domain_sheet(self, domain: str):
        sht = self._sheet(domain)
        sht.clear()
        row = self._write_header(sht, 1)
        first_data_row = row

        by_city = self.store.by_domain(domain)
        for city in sorted(by_city):
            row = self._write_banner(sht, row, city, CLR_CITY_BAN)
            for listing in by_city[city]:
                row = self._write_listing_row(sht, row, listing)

        last_data_row = row - 1
        self._apply_action_validation(
            sht, first_data_row, last_data_row, ACTIONS_ACTIVE
        )
        self._finalize_sheet(sht)

    def write_all_domain_sheets(self, domains: List[str]):
        for d in domains:
            self.write_domain_sheet(d)

    # ════════════════════════════════════════════════════════════════
    #  Selected / Discarded
    # ════════════════════════════════════════════════════════════════

    def write_selected_sheet(self):
        self._write_grouped_sheet(
            "Selected", self.store.selected_grouped(),
            CLR_SELECTED, ACTIONS_SELECTED,
        )

    def write_discarded_sheet(self):
        self._write_grouped_sheet(
            "Discarded", self.store.discarded_grouped(),
            CLR_DISCARDED, ACTIONS_DISCARDED,
        )

    def _write_grouped_sheet(self, name, grouped, accent, actions):
        sht = self._sheet(name)
        sht.clear()
        row = self._write_header(sht, 1)
        first_data_row = row
        for domain in sorted(grouped):
            row = self._write_banner(sht, row, f"● {domain}", CLR_DOMAIN_BAN)
            for city in sorted(grouped[domain]):
                row = self._write_banner(sht, row, f"    {city}", CLR_CITY_BAN)
                for listing in grouped[domain][city]:
                    row = self._write_listing_row(sht, row, listing)
        last_data_row = row - 1
        self._apply_action_validation(sht, first_data_row, last_data_row, actions)
        self._finalize_sheet(sht)

    # ════════════════════════════════════════════════════════════════
    #  Action processing
    # ════════════════════════════════════════════════════════════════

    def process_actions(self, domains: List[str]):
        """Scan every visible sheet for Action-column entries and apply them."""
        sheets_to_scan = list(domains) + ["Selected", "Discarded"]
        existing = {s.name for s in self.wb.sheets}
        count = 0
        for name in sheets_to_scan:
            if name not in existing:
                continue
            count += self._process_sheet_actions(self.wb.sheets[name])
        if count:
            self.store.save()
            logger.info(f"Processed {count} actions")

    def _process_sheet_actions(self, sht) -> int:
        last_row = sht.used_range.last_cell.row
        if last_row < 2:
            return 0
        actions_taken = 0
        for r in range(2, last_row + 1):
            action = sht.range((r, COL_ACTION)).value
            lid    = sht.range((r, COL_ID)).value
            if not action or not lid:
                continue
            act = str(action).strip().lower()
            lid = str(lid).strip()
            if lid not in self.store.listings:
                continue
            if   act == "select":  self.store.select_listing(lid);  actions_taken += 1
            elif act == "discard": self.store.discard_listing(lid); actions_taken += 1
            elif act == "restore": self.store.restore_listing(lid); actions_taken += 1
            elif act == "message": self._handle_message(lid);       actions_taken += 1
            else:
                continue
            sht.range((r, COL_ACTION)).value = None
        return actions_taken

    def _handle_message(self, listing_id: str):
        listing = self.store.listings.get(listing_id)
        if not listing:
            return

        # If a thread already exists, just refresh its state.
        tid = listing.email_thread_id or self.threads.get(listing_id)
        if tid:
            ref = self.email.get_thread(tid)
            if ref:
                self.store.set_email_thread(
                    listing_id, ref.thread_id, has_unread=ref.has_unread
                )
            return

        # Otherwise, create a new draft.
        settings = get_settings().outlook  # rename to .mail later if you like
        req = DraftRequest(
            to=listing.metadata.contact_email or "",
            subject=settings.default_subject_template.format(address=listing.address),
            body=settings.default_body_template.format(address=listing.address),
            listing_id=listing_id,
        )
        try:
            ref = self.email.create_draft(req)
        except NotImplementedError:
            logger.warning("Email client not configured; message action skipped.")
            return

        self.threads.set(listing_id, ref.thread_id)
        self.store.set_email_thread(
            listing_id, ref.thread_id, has_unread=ref.has_unread
        )

    def _open_compose(self, listing: RentalListing) -> bool:
        settings = get_settings().outlook
        subject = settings.default_subject_template.format(address=listing.address)
        body    = settings.default_body_template.format(address=listing.address)
        to_addr = listing.metadata.contact_email or ""

        if _HAS_OUTLOOK:
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                mail = outlook.CreateItem(0)  # olMailItem
                mail.To = to_addr
                mail.Subject = subject
                mail.Body = body
                mail.Display(False)
                return True
            except Exception as exc:
                logger.warning(f"Outlook compose failed: {exc}")

        # Fallback: shell out a mailto link
        try:
            import webbrowser
            q = urllib.parse.urlencode({"subject": subject, "body": body})
            webbrowser.open(f"mailto:{to_addr}?{q}")
            return False
        except Exception:
            return False
        

    # ════════════════════════════════════════════════════════════════
    #  Low-level writers
    # ════════════════════════════════════════════════════════════════

    def _sheet(self, name: str):
        for s in self.wb.sheets:
            if s.name == name:
                return s
        return self.wb.sheets.add(name, after=self.wb.sheets[-1])

    @staticmethod
    def _write_header(sht, row: int) -> int:
        for c, hdr in enumerate(COL_HEADERS, 1):
            cell = sht.range((row, c))
            cell.value = hdr
            cell.font.bold = True
            cell.font.color = CLR_WHITE
        sht.range((row, 1), (row, NUM_COLS)).color = CLR_HEADER
        return row + 1

    @staticmethod
    def _write_banner(sht, row: int, text: str, colour: tuple) -> int:
        rng = sht.range((row, 1), (row, NUM_COLS))
        rng.merge()
        rng.value = text
        rng.color = colour
        rng.font.color = CLR_WHITE
        rng.font.bold = True
        rng.font.size = 12
        rng.row_height = 26
        return row + 1

    def _write_listing_row(self, sht, row: int, listing: RentalListing) -> int:
        flat = listing.to_excel_row()
        for c, (header, key) in enumerate(COLUMNS, 1):
            cell = sht.range((row, c))
            if key is None:
                continue  # Action column — user input
            val = flat.get(key, "")

            if key == "URL" and val:
                # Set a safe text value first, then attempt the hyperlink.
                cell.value = "🔗 Open"
                try:
                    cell.add_hyperlink(str(val), text_to_display="🔗 Open")
                except Exception:
                    cell.value = val
            elif key == "Email":
                if val:
                    cell.value = "📧"
                    try:
                        cell.add_hyperlink(
                            self.email.web_url_for(listing.email_thread_id or ""),
                            text_to_display="📧",
                        )
                    except Exception:
                        pass
                else:
                    cell.value = ""
            elif key == "Unread":
                cell.value = "🔔" if val else ""
            elif isinstance(val, datetime):
                cell.value = val
                cell.number_format = "yyyy-mm-dd hh:mm"
            elif isinstance(val, bool):
                cell.value = "✓" if val else ""
            else:
                cell.value = val
        return row + 1

    @staticmethod
    def _apply_action_validation(sht, first_row: int, last_row: int, csv: str):
        """Excel data-validation dropdown on the Action column."""
        if last_row < first_row:
            return
        try:
            rng = sht.range((first_row, COL_ACTION), (last_row, COL_ACTION))
            # xlValidateList = 3; xlValidAlertStop = 1; xlBetween = 1
            rng.api.Validation.Delete()
            rng.api.Validation.Add(Type=3, AlertStyle=1, Operator=1, Formula1=csv)
            rng.api.Validation.IgnoreBlank = True
            rng.api.Validation.InCellDropdown = True
        except Exception as exc:
            logger.debug(f"Validation skipped on {sht.name}: {exc}")

    @staticmethod
    def _finalize_sheet(sht):
        try:
            sht.autofit("c")
            # Freeze the header row.
            sht.api.Activate()
            sht.book.app.api.ActiveWindow.SplitRow = 1
            sht.book.app.api.ActiveWindow.FreezePanes = True
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
#  UDF
# ════════════════════════════════════════════════════════════════

@xw.func
def refresh_view(caller) -> str:
    """``=refresh_view()`` — process pending actions and redraw all sheets."""
    iface = ExcelInterface(workbook=caller.sheet.book)
    settings = get_settings()
    iface.refresh_all(settings.enabled_sites, settings.search)
    return f"Refreshed at {datetime.now():%H:%M:%S}"