"""
xlwings-based Excel interface for the Rental Aggregator.

Performance notes
─────────────────
• Every data sheet is written in a single bulk ``range.value = grid``
  call, followed by a small number of formatting passes (banners,
  date columns, hyperlinks).  This keeps COM round-trips under ~400
  per sheet regardless of listing count.
• ``action_on_selection`` performs a **targeted refresh** that only
  rewrites the 2–3 sheets affected by the action, not all 5+.
• Notes are harvested with one bulk-read per sheet before any rewrite
  so that user edits are never lost.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import xlwings as xw

try:
    import win32com.client
    _HAS_OUTLOOK = True
except ImportError:
    _HAS_OUTLOOK = False

from config.settings import SearchParameters, get_settings
from data.models import RentalListing
from data.store import ListingStore
from mail import EmailClient, DraftRequest, ThreadIndex
from mail.gmail_client import NullEmailClient

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ensure_logging():
    root = logging.getLogger()
    if any(getattr(h, "_ra_file", False) for h in root.handlers):
        return
    handler = logging.FileHandler(
        PROJECT_ROOT / "rental_aggregator.log", encoding="utf-8",
    )
    handler._ra_file = True
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


# ────────────────────────────────────────────────────────────────────
#  Column schema
# ────────────────────────────────────────────────────────────────────

COLUMNS: List[Tuple[str, str]] = [
    ("ID",         "ID"),
    ("Source",     "Source"),
    ("Title",      "Title"),
    ("Address",    "Address"),
    ("City",       "City"),
    ("Price",      "Price"),
    ("Beds",       "Beds"),
    ("Baths",      "Baths"),
    ("Sq.Ft.",     "Sq.Ft."),
    ("Type",       "Type"),
    ("Heating",    "Heating"),
    ("Heat Incl.", "Heat Incl."),
    ("A/C",        "A/C"),
    ("Laundry",    "Laundry"),
    ("Parking",    "Parking"),
    ("Pets",       "Pets"),
    ("Balcony",    "Balcony"),
    ("Gym",        "Gym"),
    ("Posted",     "Posted"),
    ("Available",  "Available"),
    ("URL",        "URL"),
    ("Email",      "Email"),
    ("Unread",     "Unread"),
    ("Notes",      "Notes"),
    ("First Seen", "First Seen"),
    ("Last Seen",  "Last Seen"),
]

COL_HEADERS = [c[0] for c in COLUMNS]
NUM_COLS    = len(COL_HEADERS)
COL_ID      = 1
COL_NOTES   = COL_HEADERS.index("Notes") + 1
COL_URL     = COL_HEADERS.index("URL") + 1
COL_EMAIL   = COL_HEADERS.index("Email") + 1

TOOLBAR_ROW = 1
HEADER_ROW  = 2
DATA_START  = 3

# Columns whose cells contain datetimes and need a number format.
_DATE_COLS = tuple(
    COL_HEADERS.index(n) + 1
    for n in ("Posted", "Available", "First Seen", "Last Seen")
)

# Fixed column widths (deterministic, avoids the expensive autofit).
_COL_WIDTHS = {
    "ID": 14, "Source": 12, "Title": 30, "Address": 35, "City": 12,
    "Price": 12, "Beds": 6, "Baths": 6, "Sq.Ft.": 8, "Type": 10,
    "Heating": 10, "Heat Incl.": 9, "A/C": 5, "Laundry": 10,
    "Parking": 10, "Pets": 5, "Balcony": 7, "Gym": 5, "Posted": 16,
    "Available": 16, "URL": 8, "Email": 8, "Unread": 7, "Notes": 25,
    "First Seen": 16, "Last Seen": 16,
}


# ────────────────────────────────────────────────────────────────────
#  Colours
# ────────────────────────────────────────────────────────────────────

CLR_HEADER      = (44,   62,  80)
CLR_TOOLBAR_BG  = (52,   73,  94)
CLR_DOMAIN_BAN  = (39,  174,  96)
CLR_CITY_BAN    = (41,  128, 185)
CLR_WHITE       = (255, 255, 255)

CLR_BTN_SELECT  = (39,  174,  96)
CLR_BTN_DISCARD = (192,  57,  43)
CLR_BTN_RESTORE = (41,  128, 185)
CLR_BTN_MESSAGE = (142,  68, 173)
CLR_BTN_REFRESH = (44,   62,  80)
CLR_BTN_SCRAPE  = (243, 156,  18)
CLR_BTN_SAVE    = (39,  174,  96)

CLR_CFG_ALT_A   = (240, 243, 247)
CLR_CFG_ALT_B   = (250, 251, 253)
CLR_CFG_BORDER  = (220, 224, 232)

_CFG_COLS = 5


# ────────────────────────────────────────────────────────────────────
#  Button definitions
# ────────────────────────────────────────────────────────────────────

BUTTONS_DOMAIN = [
    ("Select",  "select",  CLR_BTN_SELECT),
    ("Discard", "discard", CLR_BTN_DISCARD),
    ("Message", "message", CLR_BTN_MESSAGE),
    ("Refresh", "refresh", CLR_BTN_REFRESH),
    ("Scrape",  "scrape",  CLR_BTN_SCRAPE),
]
BUTTONS_SELECTED = [
    ("Discard", "discard", CLR_BTN_DISCARD),
    ("Restore", "restore", CLR_BTN_RESTORE),
    ("Message", "message", CLR_BTN_MESSAGE),
    ("Refresh", "refresh", CLR_BTN_REFRESH),
]
BUTTONS_DISCARDED = [
    ("Select",  "select",  CLR_BTN_SELECT),
    ("Restore", "restore", CLR_BTN_RESTORE),
    ("Refresh", "refresh", CLR_BTN_REFRESH),
]
BUTTONS_CONFIG = [
    ("Save",    "save_config", CLR_BTN_SAVE),
    ("Refresh", "refresh",     CLR_BTN_REFRESH),
    ("Scrape",  "scrape",      CLR_BTN_SCRAPE),
]


def _rgb_to_ole(rgb: tuple) -> int:
    return rgb[0] + (rgb[1] * 256) + (rgb[2] * 65536)


# ────────────────────────────────────────────────────────────────────
#  Scrape state
# ────────────────────────────────────────────────────────────────────

_run_lock = threading.Lock()
_last_summary: str = "Idle"


def _set_status(msg: str):
    global _last_summary
    _last_summary = msg


# ════════════════════════════════════════════════════════════════════
#  Module-level entry points
# ════════════════════════════════════════════════════════════════════

def button_action(action: str):
    _ensure_logging()
    try:
        wb = xw.Book.caller()
        iface = ExcelInterface(workbook=wb)
        settings = get_settings()

        if action == "refresh":
            iface.refresh_all(settings.enabled_sites)
        elif action == "save_config":
            iface.save_config_from_sheet()
            iface.write_config_sheet()
        elif action == "scrape":
            _start_scrape(wb)
        elif action in ("select", "discard", "restore", "message"):
            iface.action_on_selection(action)
        else:
            logger.warning("Unknown button action: %s", action)
    except Exception:
        logger.exception("button_action(%s) failed", action)
        raise


def _start_scrape(wb: xw.Book):
    if not _run_lock.acquire(blocking=False):
        try:
            wb.app.api.StatusBar = (
                f"Scrape already running — {_last_summary}"
            )
        except Exception:
            pass
        return

    def _worker():
        try:
            _set_status("Running…")
            from run_scrapers import run_all_scrapers, ingest
            started = datetime.now()
            settings = get_settings()
            listings = run_all_scrapers(settings)
            summary = ingest(listings)
            elapsed = (datetime.now() - started).total_seconds()
            _set_status(
                f"Done {datetime.now():%H:%M:%S} "
                f"({elapsed:.0f}s) — {summary}"
            )
        except Exception as exc:
            _set_status(f"Error: {exc}")
            logger.exception("scrape worker failed")
        finally:
            _run_lock.release()

    _set_status("Starting…")
    try:
        wb.app.api.StatusBar = "Scrape started — click Refresh when done"
    except Exception:
        pass
    threading.Thread(target=_worker, daemon=True).start()


@xw.func
def scrape_status() -> str:
    return _last_summary


# ════════════════════════════════════════════════════════════════════
#  ExcelInterface
# ════════════════════════════════════════════════════════════════════

class ExcelInterface:

    def __init__(
        self,
        workbook: Optional[xw.Book] = None,
        store: Optional[ListingStore] = None,
        email_client: Optional[EmailClient] = None,
        thread_index: Optional[ThreadIndex] = None,
    ):
        self.wb = workbook or xw.Book.caller()
        self.store = store or ListingStore()
        self.email = email_client or NullEmailClient()
        self.threads = thread_index or ThreadIndex()

    # ================================================================
    #  Refresh operations
    # ================================================================

    def refresh_all(self, domains: List[str]):
        """Full refresh — every sheet rewritten from the store."""
        self.store.load()
        all_data_sheets = list(domains) + ["Selected", "Discarded"]
        self._harvest_notes(all_data_sheets)

        try:
            restore = self.wb.app.api.ActiveSheet.Name
        except Exception:
            restore = None

        app = self.wb.app
        app.screen_updating = False
        try:
            self.write_config_sheet()
            self.write_all_domain_sheets(domains)
            self.write_selected_sheet()
            self.write_discarded_sheet()
        finally:
            app.screen_updating = True

        if restore:
            try:
                self.wb.sheets[restore].activate()
            except Exception:
                pass

    def _targeted_refresh(
        self,
        source_name: str,
        action: str,
        affected_domains: Set[str],
        all_domains: List[str],
    ):
        """Rewrite only the 2–3 sheets the action actually touched."""
        self.store.load()

        # Determine the minimal dirty set.
        dirty: Set[str] = {source_name}
        if action == "select":
            dirty.add("Selected")
        elif action == "discard":
            dirty.add("Discarded")
        elif action == "restore":
            # Restored listings return to their domain sheets.
            dirty.update(affected_domains)

        self._harvest_notes(list(dirty))

        try:
            restore = self.wb.app.api.ActiveSheet.Name
        except Exception:
            restore = None

        app = self.wb.app
        app.screen_updating = False
        try:
            for name in dirty:
                if name == "Selected":
                    self.write_selected_sheet()
                elif name == "Discarded":
                    self.write_discarded_sheet()
                elif name in all_domains:
                    self.write_domain_sheet(name)
        finally:
            app.screen_updating = True

        if restore:
            try:
                self.wb.sheets[restore].activate()
            except Exception:
                pass

    # ================================================================
    #  Action processing
    # ================================================================

    def action_on_selection(self, action: str):
        try:
            sel = self.wb.app.selection
            if sel is None:
                return
            sht = sel.sheet
        except Exception:
            return

        if sht.name == "Config":
            return

        listing_ids = self._ids_from_selection(sel, sht)
        if not listing_ids:
            return

        affected_domains: Set[str] = set()
        count = 0
        for lid in listing_ids:
            listing = self.store.listings.get(lid)
            if not listing:
                continue
            affected_domains.add(listing.metadata.source_site)
            if action == "select":
                self.store.select_listing(lid)
                count += 1
            elif action == "discard":
                self.store.discard_listing(lid)
                count += 1
            elif action == "restore":
                self.store.restore_listing(lid)
                count += 1
            elif action == "message":
                self._handle_message(lid)
                count += 1

        if count:
            self.store.save()
            settings = get_settings()
            self._targeted_refresh(
                sht.name, action,
                affected_domains, settings.enabled_sites,
            )

    # ================================================================
    #  Config sheet  (small — no bulk-write needed)
    # ================================================================

    def write_config_sheet(
        self, params: Optional[SearchParameters] = None,
    ):
        params = params or get_settings().search
        sht = self._sheet("Config")
        sht.clear()
        self._clear_shapes(sht)

        all_rows = params.to_excel_rows()
        mid = (len(all_rows) + 1) // 2
        left_rows = all_rows[:mid]
        right_rows = all_rows[mid:]

        self._add_toolbar(sht, BUTTONS_CONFIG, bg_cols=_CFG_COLS)
        self._write_config_block_header(sht, HEADER_ROW, 1, 2)
        for i, (label, val) in enumerate(left_rows):
            r = DATA_START + i
            sht.range((r, 1)).value = label
            sht.range((r, 2)).value = val
            self._style_config_row(sht, r, 1, 2, i % 2 == 0)

        self._write_config_block_header(sht, HEADER_ROW, 4, 5)
        for i, (label, val) in enumerate(right_rows):
            r = DATA_START + i
            sht.range((r, 4)).value = label
            sht.range((r, 5)).value = val
            self._style_config_row(sht, r, 4, 5, i % 2 == 0)

        status_row = DATA_START + max(len(left_rows), len(right_rows)) + 1
        lbl = sht.range((status_row, 1))
        lbl.value = "Scrape Status:"
        lbl.font.bold = True
        lbl.font.size = 11
        sht.range((status_row, 2)).formula = "=scrape_status()"
        sht.range((status_row, 2), (status_row, 5)).merge()

        sht.range("A:A").column_width = 24
        sht.range("B:B").column_width = 30
        sht.range("C:C").column_width = 3
        sht.range("D:D").column_width = 24
        sht.range("E:E").column_width = 30

    def read_config(self) -> SearchParameters:
        sht = self.wb.sheets["Config"]
        rows: List[Tuple[str, object]] = []
        for col_lbl, col_val in [(1, 2), (4, 5)]:
            r = DATA_START
            while True:
                label = sht.range((r, col_lbl)).value
                if not label:
                    break
                rows.append(
                    (str(label).strip(), sht.range((r, col_val)).value)
                )
                r += 1
        return SearchParameters.from_excel_rows(rows)

    def save_config_from_sheet(self):
        try:
            params = self.read_config()
            settings = get_settings()
            settings.search = params
            settings.save()
            import config.settings as _cs
            _cs._settings = None
        except Exception as exc:
            logger.warning("Config save failed: %s", exc)

    # ================================================================
    #  Domain sheets  (bulk path)
    # ================================================================

    def write_domain_sheet(self, domain: str):
        sht = self._sheet(domain)
        sht.clear()
        self._clear_shapes(sht)
        self._add_toolbar(sht, BUTTONS_DOMAIN)
        self._write_header(sht, HEADER_ROW)

        by_city = self.store.by_domain(domain)
        self._flush_block(sht, *self._build_city_block(by_city))
        self._finalize_sheet(sht)

    def write_all_domain_sheets(self, domains: List[str]):
        for d in domains:
            self.write_domain_sheet(d)

    # ================================================================
    #  Selected / Discarded  (bulk path)
    # ================================================================

    def write_selected_sheet(self):
        self._write_grouped_sheet(
            "Selected",
            self.store.selected_grouped(),
            BUTTONS_SELECTED,
        )

    def write_discarded_sheet(self):
        self._write_grouped_sheet(
            "Discarded",
            self.store.discarded_grouped(),
            BUTTONS_DISCARDED,
        )

    def _write_grouped_sheet(self, name, grouped, buttons):
        sht = self._sheet(name)
        sht.clear()
        self._clear_shapes(sht)
        self._add_toolbar(sht, buttons)
        self._write_header(sht, HEADER_ROW)

        self._flush_block(sht, *self._build_grouped_block(grouped))
        self._finalize_sheet(sht)

    # ================================================================
    #  Bulk-data builders  (pure Python — zero COM calls)
    # ================================================================

    def _build_city_block(self, by_city):
        """Return ``(grid, banners, urls, emails)`` for a domain sheet."""
        grid:    List[list] = []
        banners: List[Tuple[int, str, tuple]] = []
        urls:    List[Tuple[int, str]] = []
        emails:  List[Tuple[int, RentalListing]] = []

        for city in sorted(by_city):
            idx = len(grid)
            grid.append(self._banner_row(city))
            banners.append((idx, city, CLR_CITY_BAN))

            for listing in by_city[city]:
                idx = len(grid)
                grid.append(self._listing_to_row(listing))
                if listing.metadata.source_url:
                    urls.append((idx, listing.metadata.source_url))
                if listing.email_thread_id:
                    emails.append((idx, listing))

        return grid, banners, urls, emails

    def _build_grouped_block(self, grouped):
        """Return ``(grid, banners, urls, emails)`` for Selected/Discarded."""
        grid:    List[list] = []
        banners: List[Tuple[int, str, tuple]] = []
        urls:    List[Tuple[int, str]] = []
        emails:  List[Tuple[int, RentalListing]] = []

        for domain in sorted(grouped):
            idx = len(grid)
            grid.append(self._banner_row(f"● {domain}"))
            banners.append((idx, f"● {domain}", CLR_DOMAIN_BAN))

            for city in sorted(grouped[domain]):
                idx = len(grid)
                grid.append(self._banner_row(f"    {city}"))
                banners.append((idx, f"    {city}", CLR_CITY_BAN))

                for listing in grouped[domain][city]:
                    idx = len(grid)
                    grid.append(self._listing_to_row(listing))
                    if listing.metadata.source_url:
                        urls.append((idx, listing.metadata.source_url))
                    if listing.email_thread_id:
                        emails.append((idx, listing))

        return grid, banners, urls, emails

    @staticmethod
    def _banner_row(text: str) -> list:
        """A row whose first cell is *text* and the rest are empty."""
        return [text] + [None] * (NUM_COLS - 1)

    @staticmethod
    def _listing_to_row(listing: RentalListing) -> list:
        """Convert a listing to a flat value list (no COM calls)."""
        flat = listing.to_excel_row()
        row: list = []
        for _hdr, key in COLUMNS:
            val = flat.get(key, "")
            if key == "URL":
                row.append("Link" if val else "")
            elif key == "Email":
                row.append("Thread" if val else "")
            elif key == "Unread":
                row.append("Yes" if val else "")
            elif isinstance(val, bool):
                row.append("Yes" if val else "")
            else:
                # datetime, int, float, str, None — all go straight
                # into the grid and are written in a single bulk call.
                row.append(val)
        return row

    # ================================================================
    #  Bulk-data writer  (the performance core)
    # ================================================================

    def _flush_block(self, sht, grid, banners, urls, emails):
        """Write a pre-built data block with minimal COM round-trips.

        Call breakdown for a sheet with N listings and B banners:
          1 bulk value write  +  ~6·B banner formats  +  4 date-column
          formats  +  len(urls) hyperlinks  +  len(emails) hyperlinks
        """
        if not grid:
            return

        n   = len(grid)
        top = DATA_START
        bot = top + n - 1

        # ── 1. Bulk data write (ONE COM call) ─────────────────────
        sht.range((top, 1), (bot, NUM_COLS)).value = grid

        # ── 2. Banner formatting (~6 calls per banner) ────────────
        for rel, _text, colour in banners:
            r = top + rel
            rng = sht.range((r, 1), (r, NUM_COLS))
            rng.merge()
            rng.color = colour
            rng.font.color = CLR_WHITE
            rng.font.bold = True
            rng.font.size = 12
            rng.row_height = 26

        # ── 3. Date-column number format (4 calls) ───────────────
        for c in _DATE_COLS:
            sht.range((top, c), (bot, c)).number_format = (
                "yyyy-mm-dd hh:mm"
            )

        # ── 4. Hyperlinks (1 call each — unavoidably individual) ──
        for rel, url in urls:
            cell = sht.range((top + rel, COL_URL))
            try:
                cell.add_hyperlink(url, text_to_display="Link")
            except Exception:
                cell.value = url

        for rel, listing in emails:
            cell = sht.range((top + rel, COL_EMAIL))
            try:
                cell.add_hyperlink(
                    self.email.web_url_for(
                        listing.email_thread_id or "",
                    ),
                    text_to_display="Thread",
                )
            except Exception:
                pass

    # ================================================================
    #  Notes harvesting  (bulk read)
    # ================================================================

    def _harvest_notes(self, sheet_names: List[str]):
        """Bulk-read the Notes column and persist any changes."""
        existing = {s.name for s in self.wb.sheets}
        changed = False
        for name in sheet_names:
            if name not in existing or name == "Config":
                continue
            sht = self.wb.sheets[name]
            last = sht.used_range.last_cell.row
            if last < DATA_START:
                continue

            # ONE COM call: read the entire data region.
            raw = sht.range(
                (DATA_START, 1), (last, NUM_COLS),
            ).value

            # Single-row ranges come back as a flat list, not nested.
            if last == DATA_START:
                raw = [raw]

            for row in raw:
                if not row or not isinstance(row, (list, tuple)):
                    continue
                lid_val   = row[COL_ID - 1]
                notes_val = row[COL_NOTES - 1]
                if not lid_val:
                    continue
                lid = str(lid_val).strip()
                notes_str = str(notes_val).strip() if notes_val else ""
                if lid in self.store.listings:
                    current = self.store.listings[lid].user_notes or ""
                    if notes_str != current:
                        self.store.set_notes(lid, notes_str)
                        changed = True
        if changed:
            self.store.save()

    # ================================================================
    #  Message handling
    # ================================================================

    def _handle_message(self, listing_id: str):
        listing = self.store.listings.get(listing_id)
        if not listing:
            return
        tid = listing.email_thread_id or self.threads.get(listing_id)
        if tid:
            ref = self.email.get_thread(tid)
            if ref:
                self.store.set_email_thread(
                    listing_id, ref.thread_id,
                    has_unread=ref.has_unread,
                )
            return
        settings = get_settings().outlook
        req = DraftRequest(
            to=listing.metadata.contact_email or "",
            subject=settings.default_subject_template.format(
                address=listing.address,
            ),
            body=settings.default_body_template.format(
                address=listing.address,
            ),
            listing_id=listing_id,
        )
        try:
            ref = self.email.create_draft(req)
        except NotImplementedError:
            self._open_compose(listing)
            return
        self.threads.set(listing_id, ref.thread_id)
        self.store.set_email_thread(
            listing_id, ref.thread_id, has_unread=ref.has_unread,
        )

    def _open_compose(self, listing: RentalListing) -> bool:
        settings = get_settings().outlook
        subject = settings.default_subject_template.format(
            address=listing.address,
        )
        body = settings.default_body_template.format(
            address=listing.address,
        )
        to_addr = listing.metadata.contact_email or ""
        if _HAS_OUTLOOK:
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                mail = outlook.CreateItem(0)
                mail.To = to_addr
                mail.Subject = subject
                mail.Body = body
                mail.Display(False)
                return True
            except Exception as exc:
                logger.warning("Outlook compose failed: %s", exc)
        try:
            import webbrowser
            q = urllib.parse.urlencode(
                {"subject": subject, "body": body},
            )
            webbrowser.open(f"mailto:{to_addr}?{q}")
            return False
        except Exception:
            return False

    # ================================================================
    #  Selection → listing IDs
    # ================================================================

    @staticmethod
    def _ids_from_selection(sel, sht) -> List[str]:
        ids: List[str] = []
        try:
            for area in sel.api.Areas:
                for row_num in range(
                    area.Row, area.Row + area.Rows.Count,
                ):
                    if row_num < DATA_START:
                        continue
                    val = sht.range((row_num, COL_ID)).value
                    if val and str(val).strip():
                        lid = str(val).strip()
                        if lid not in ids:
                            ids.append(lid)
        except Exception as exc:
            logger.debug("Selection parse error: %s", exc)
        return ids

    # ================================================================
    #  Toolbar
    # ================================================================

    def _add_toolbar(
        self, sht, button_defs, *, bg_cols: int = NUM_COLS,
    ):
        sht.range("1:1").row_height = 42
        sht.range((1, 1), (1, bg_cols)).color = CLR_TOOLBAR_BG

        btn_w, btn_h = 90, 28
        spacing = 8
        top = 7
        left = 10.0

        for label, action, colour in button_defs:
            shp = sht.api.Shapes.AddShape(
                5, left, top, btn_w, btn_h,
            )
            shp.Name = f"btn_{action}"
            shp.Fill.ForeColor.RGB = _rgb_to_ole(colour)
            shp.Line.Visible = False
            tf = shp.TextFrame2
            tf.TextRange.Text = label
            tf.TextRange.Font.Fill.ForeColor.RGB = _rgb_to_ole(CLR_WHITE)
            tf.TextRange.Font.Size = 10
            tf.TextRange.Font.Bold = True
            tf.TextRange.ParagraphFormat.Alignment = 2
            tf.VerticalAnchor = 3
            shp.OnAction = f"RA_{action}"
            left += btn_w + spacing

    # ================================================================
    #  Low-level helpers
    # ================================================================

    def _sheet(self, name: str):
        for s in self.wb.sheets:
            if s.name == name:
                return s
        return self.wb.sheets.add(name, after=self.wb.sheets[-1])

    @staticmethod
    def _clear_shapes(sht):
        try:
            for i in range(sht.api.Shapes.Count, 0, -1):
                sht.api.Shapes.Item(i).Delete()
        except Exception:
            pass

    @staticmethod
    def _write_header(sht, row: int):
        """Single bulk write for the header row + formatting."""
        rng = sht.range((row, 1), (row, NUM_COLS))
        rng.value = [COL_HEADERS]
        rng.font.bold = True
        rng.font.color = CLR_WHITE
        rng.color = CLR_HEADER

    @staticmethod
    def _finalize_sheet(sht):
        """Fixed column widths + freeze the toolbar and header rows."""
        try:
            for i, hdr in enumerate(COL_HEADERS, 1):
                sht.range((HEADER_ROW, i)).column_width = (
                    _COL_WIDTHS.get(hdr, 12)
                )
            sht.api.Activate()
            win = sht.book.app.api.ActiveWindow
            win.FreezePanes = False
            win.SplitRow = 0
            win.SplitColumn = 0
            sht.range((DATA_START, 1)).api.Select()
            win.FreezePanes = True
        except Exception:
            pass

    # ── Config styling ─────────────────────────────────────────────

    @staticmethod
    def _write_config_block_header(sht, row, col_lbl, col_val):
        for c, txt in ((col_lbl, "Parameter"), (col_val, "Value")):
            cell = sht.range((row, c))
            cell.value = txt
            cell.font.bold = True
            cell.font.color = CLR_WHITE
            cell.font.size = 11
        sht.range((row, col_lbl), (row, col_val)).color = CLR_HEADER

    @staticmethod
    def _style_config_row(sht, row, col_lbl, col_val, alternate):
        label_cell = sht.range((row, col_lbl))
        value_cell = sht.range((row, col_val))
        rng = sht.range((row, col_lbl), (row, col_val))
        label_cell.font.bold = True
        label_cell.color = CLR_CFG_ALT_A if alternate else CLR_CFG_ALT_B
        value_cell.color = CLR_WHITE
        try:
            border = rng.api.Borders(9)
            border.LineStyle = 1
            border.Weight = 1
            border.Color = _rgb_to_ole(CLR_CFG_BORDER)
        except Exception:
            pass