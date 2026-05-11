"""
xlwings-based Excel interface for the Rental Aggregator.

Provides:
  • Config sheet  ← read / write search parameters
  • Per-domain sheets  ← listings organised by jurisdiction with banners
  • Selected / Discarded sheets  ← organised by domain + jurisdiction
  • Action processing  ← select / discard / message
  • Email-thread indicators (📧 / 🔔) and hyperlink columns

All public functions are safe to call from VBA via ``RunPython``.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    import xlwings as xw
except ImportError:
    xw = None  # Allow imports for structure even without xlwings

from config.settings import SearchParameters, get_settings
from data.models import RentalListing
from data.store import ListingStore

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
#  Column schema  (display header → listing dict key)
# ────────────────────────────────────────────────────────────────────

COLUMNS = [
    ("Action",      None),          # User fills: select / discard / message
    ("ID",          "ID"),
    ("Source",      "Source"),
    ("Title",       "Title"),
    ("Address",     "Address"),
    ("City",        "City"),
    ("Price",       "Price"),
    ("Adj. Rent",   "Adj. Rent"),
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
NUM_COLS = len(COL_HEADERS)

# ────────────────────────────────────────────────────────────────────
#  Colours  (R, G, B)
# ────────────────────────────────────────────────────────────────────
CLR_HEADER      = (44,  62,  80)
CLR_DOMAIN_BAN  = (39, 174,  96)
CLR_CITY_BAN    = (41, 128, 185)
CLR_SELECTED    = (39, 174,  96)
CLR_DISCARDED   = (192,  57,  43)
CLR_WHITE       = (255, 255, 255)


class ExcelInterface:
    """Manages all reads/writes between the workbook and the data store."""

    def __init__(
        self,
        workbook: Optional["xw.Book"] = None,
        workbook_path: Optional[str] = None,
        store: Optional[ListingStore] = None,
    ):
        if workbook is not None:
            self.wb = workbook
        elif workbook_path:
            self.wb = xw.Book(workbook_path)
        else:
            self.wb = xw.Book.caller()
        self.store = store or ListingStore()

    # ────────────────────────────────────────────────────────────
    #  Config sheet
    # ────────────────────────────────────────────────────────────

    def write_config(self, params: Optional[SearchParameters] = None):
        params = params or get_settings().search
        sht = self._sheet("Config")
        sht.clear()
        sht.range("A1").value = "Parameter"
        sht.range("B1").value = "Value"
        sht.range("A1:B1").font.bold = True
        sht.range("A1:B1").color = CLR_HEADER
        sht.range("A1:B1").font.color = CLR_WHITE

        for i, (label, val) in enumerate(params.to_excel_rows(), start=2):
            sht.range(f"A{i}").value = label
            sht.range(f"B{i}").value = val
        sht.autofit("c")

    def read_config(self) -> SearchParameters:
        sht = self.wb.sheets["Config"]
        rows = []
        r = 2
        while True:
            label = sht.range(f"A{r}").value
            if not label:
                break
            rows.append((label, sht.range(f"B{r}").value))
            r += 1
        return SearchParameters.from_excel_rows(rows)

    # ────────────────────────────────────────────────────────────
    #  Domain sheets
    # ────────────────────────────────────────────────────────────

    def write_domain_sheet(self, domain: str):
        """Write active listings for *domain*, grouped by city."""
        sht = self._sheet(domain)
        sht.clear()
        row = self._write_header(sht, 1)

        by_city = self.store.by_domain(domain)
        for city in sorted(by_city):
            row = self._write_banner(sht, row, city, CLR_CITY_BAN)
            for listing in by_city[city]:
                row = self._write_listing_row(sht, row, listing)
        sht.autofit("c")

    def write_all_domain_sheets(self, domains: List[str]):
        for d in domains:
            self.write_domain_sheet(d)

    # ────────────────────────────────────────────────────────────
    #  Selected / Discarded sheets
    # ────────────────────────────────────────────────────────────

    def write_selected_sheet(self):
        self._write_grouped_sheet(
            "Selected",
            self.store.selected_grouped(),
            CLR_SELECTED,
        )

    def write_discarded_sheet(self):
        self._write_grouped_sheet(
            "Discarded",
            self.store.discarded_grouped(),
            CLR_DISCARDED,
        )

    def _write_grouped_sheet(
        self,
        sheet_name: str,
        grouped: Dict[str, Dict[str, List[RentalListing]]],
        accent_colour: tuple,
    ):
        sht = self._sheet(sheet_name)
        sht.clear()
        row = self._write_header(sht, 1)

        for domain in sorted(grouped):
            row = self._write_banner(sht, row, f"● {domain}", CLR_DOMAIN_BAN)
            for city in sorted(grouped[domain]):
                row = self._write_banner(sht, row, f"    {city}", CLR_CITY_BAN)
                for listing in grouped[domain][city]:
                    row = self._write_listing_row(sht, row, listing)
        sht.autofit("c")

    # ────────────────────────────────────────────────────────────
    #  Action processing
    # ────────────────────────────────────────────────────────────

    def process_actions(self, domains: List[str]):
        """
        Read the Action column on every domain sheet, execute actions,
        then rewrite all sheets.
        """
        actions_taken = 0
        for domain in domains:
            try:
                sht = self.wb.sheets[domain]
            except Exception:
                continue
            r = 2
            while True:
                cell_action = sht.range(f"A{r}").value
                cell_id = sht.range(f"B{r}").value
                if cell_id is None and cell_action is None:
                    break
                if cell_action and cell_id:
                    act = str(cell_action).strip().lower()
                    lid = str(cell_id).strip()
                    if act == "select":
                        self.store.select_listing(lid)
                        actions_taken += 1
                    elif act == "discard":
                        self.store.discard_listing(lid)
                        actions_taken += 1
                    elif act == "message":
                        self._handle_message_action(lid)
                        actions_taken += 1
                r += 1

        if actions_taken:
            self.store.save()
            self.refresh_all_sheets(domains)
            logger.info(f"Processed {actions_taken} actions")

    def _handle_message_action(self, listing_id: str):
        """
        Stub for email/message handling.

        In production this would launch an Outlook compose window or
        mark the thread; for now it just records the intent.
        """
        listing = self.store.listings.get(listing_id)
        if not listing:
            return
        if not listing.email_thread_id:
            # Generate a placeholder thread ID
            listing.email_thread_id = f"thread_{listing_id[:8]}"
        logger.info(
            f"Message action for {listing_id}: thread={listing.email_thread_id}"
        )

    # ────────────────────────────────────────────────────────────
    #  Refresh everything
    # ────────────────────────────────────────────────────────────

    def refresh_all_sheets(self, domains: List[str]):
        self.write_all_domain_sheets(domains)
        self.write_selected_sheet()
        self.write_discarded_sheet()

    # ────────────────────────────────────────────────────────────
    #  Low-level writing helpers
    # ────────────────────────────────────────────────────────────

    def _sheet(self, name: str) -> "xw.Sheet":
        """Return existing sheet or create a new one."""
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
        rng.row_height = 28
        return row + 1

    @staticmethod
    def _write_listing_row(sht, row: int, listing: RentalListing) -> int:
        flat = listing.to_excel_row()
        for c, (header, key) in enumerate(COLUMNS, 1):
            cell = sht.range((row, c))
            if key is None:
                # Action column — leave empty for user input
                continue
            val = flat.get(key, "")

            # Special rendering
            if key == "URL" and val:
                try:
                    sht.range((row, c)).add_hyperlink(str(val), text_to_display="🔗 Open")
                except Exception:
                    cell.value = val
            elif key == "Email":
                cell.value = "📧" if val else ""
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


# ────────────────────────────────────────────────────────────────────
#  Entry points for RunPython (VBA macros)
# ────────────────────────────────────────────────────────────────────

def refresh_data():
    """
    Called from VBA:
        Sub RefreshData()
            RunPython "import excel.interface; excel.interface.refresh_data()"
        End Sub
    """
    iface = ExcelInterface()
    settings = get_settings()
    iface.write_config(settings.search)
    iface.refresh_all_sheets(settings.enabled_sites)


def process_actions():
    """
    Called from VBA:
        Sub ProcessActions()
            RunPython "import excel.interface; excel.interface.process_actions()"
        End Sub
    """
    iface = ExcelInterface()
    settings = get_settings()
    iface.process_actions(settings.enabled_sites)