"""
xlwings Excel interface — Form Control checkboxes for booleans,
dynamic visible columns, on-site inquiry dispatch for realtor.ca.
"""

from __future__ import annotations

import logging, threading, urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import xlwings as xw

try:
    import win32com.client
    _HAS_OUTLOOK = True
except ImportError:
    _HAS_OUTLOOK = False

from config.settings import (
    SearchParameters, get_settings, ALL_DATA_COLUMNS,
)
from data.models import RentalListing
from data.store import ListingStore
from mail import EmailClient, DraftRequest, ThreadIndex
from mail.gmail_client import NullEmailClient

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _ensure_logging():
    root = logging.getLogger()
    if any(getattr(h, "_ra_file", False) for h in root.handlers): return
    handler = logging.FileHandler(
        PROJECT_ROOT / "rental_aggregator.log", encoding="utf-8")
    handler._ra_file = True
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler); root.setLevel(logging.INFO)

# ── Colours ────────────────────────────────────────────────────────

CLR_HEADER     = (44,  62,  80); CLR_TOOLBAR_BG = (52,  73,  94)
CLR_DOMAIN_BAN = (39, 174,  96); CLR_CITY_BAN   = (41, 128, 185)
CLR_WHITE      = (255, 255, 255)
CLR_BTN = {"select": (39,174,96), "discard": (192,57,43),
           "restore": (41,128,185), "message": (142,68,173),
           "refresh": (44,62,80), "scrape": (243,156,18),
           "save_config": (39,174,96)}
CLR_CFG_ALT_A = (240,243,247); CLR_CFG_ALT_B = (250,251,253)
CLR_CFG_BORDER = (220,224,232)

_CFG_COLS = 5
TOOLBAR_ROW = 1; HEADER_ROW = 2; DATA_START = 3
_BTN_W, _BTN_H, _BTN_GAP, _BTN_TOP = 90, 28, 8, 7

_COL_WIDTHS = {
    "ID": 14, "Address": 35, "City": 12, "Price": 12, "Beds": 6,
    "Baths": 6, "Sq.Ft.": 8, "Type": 10, "Heating": 10,
    "Heat Incl.": 9, "A/C": 5, "Laundry": 10, "Parking": 10,
    "Pets": 5, "Balcony": 7, "Gym": 5, "Posted": 16, "Available": 16,
    "URL": 8, "Email": 8, "Unread": 7, "Notes": 25,
    "First Seen": 16, "Last Seen": 16,
}
_DATE_NAMES = {"Posted", "Available", "First Seen", "Last Seen"}

BUTTONS_DOMAIN    = [("Select","select"),("Discard","discard"),
                     ("Message","message"),("Refresh","refresh"),("Scrape","scrape")]
BUTTONS_SELECTED  = [("Discard","discard"),("Restore","restore"),
                     ("Message","message"),("Refresh","refresh")]
BUTTONS_DISCARDED = [("Select","select"),("Restore","restore"),("Refresh","refresh")]
BUTTONS_CONFIG    = [("Save","save_config"),("Refresh","refresh"),("Scrape","scrape")]

def _rgb(t): return t[0] + t[1] * 256 + t[2] * 65536

_run_lock = threading.Lock()
_last_summary = "Idle"
def _set_status(msg): global _last_summary; _last_summary = msg

# ════════════════════════════════════════════════════════════════════
#  Module entry points
# ════════════════════════════════════════════════════════════════════

def button_action(action):
    _ensure_logging()
    try:
        wb = xw.Book.caller(); iface = ExcelInterface(workbook=wb)
        settings = get_settings()
        if action == "refresh":       iface.refresh_all(settings.enabled_sites)
        elif action == "save_config": iface.save_config_from_sheet(); iface.write_config_sheet()
        elif action == "scrape":      _start_scrape(wb)
        elif action in ("select", "discard", "restore", "message"):
            iface.action_on_selection(action)
    except Exception:
        logger.exception("button_action(%s) failed", action); raise

def _start_scrape(wb):
    if not _run_lock.acquire(blocking=False):
        try: wb.app.api.StatusBar = f"Scrape already running — {_last_summary}"
        except Exception: pass
        return
    def _worker():
        try:
            _set_status("Running…")
            from run_scrapers import run_all_scrapers, ingest
            started = datetime.now(); settings = get_settings()
            listings = run_all_scrapers(settings); summary = ingest(listings)
            elapsed = (datetime.now() - started).total_seconds()
            _set_status(f"Done {datetime.now():%H:%M:%S} ({elapsed:.0f}s) — {summary}")
        except Exception as exc:
            _set_status(f"Error: {exc}"); logger.exception("scrape failed")
        finally: _run_lock.release()
    _set_status("Starting…")
    try: wb.app.api.StatusBar = "Scrape started — click Refresh when done"
    except Exception: pass
    threading.Thread(target=_worker, daemon=True).start()

@xw.func
def scrape_status(): return _last_summary

# ════════════════════════════════════════════════════════════════════

class ExcelInterface:

    def __init__(self, workbook=None, store=None, email_client=None,
                 thread_index=None):
        self.wb = workbook or xw.Book.caller()
        self.store = store or ListingStore()
        self.email = email_client or NullEmailClient()
        self.threads = thread_index or ThreadIndex()

    # ── Dynamic columns ────────────────────────────────────────────

    def _columns(self):
        vis = set(get_settings().visible_columns)
        cols = [("ID", "ID")]
        for h in ALL_DATA_COLUMNS:
            if h in vis: cols.append((h, h))
        return cols

    def _col_idx(self, cols, name):
        try: return [c[0] for c in cols].index(name) + 1
        except ValueError: return None

    # ── Checkbox helper ────────────────────────────────────────────

    @staticmethod
    def _add_checkbox(sht, row, col, checked):
        """Overlay a Form Control checkbox on a cell's boolean value.

        The cell retains its real TRUE/FALSE value (linked to the
        checkbox); the number format hides the redundant text so only
        the checkbox is visible.  Uses AddFormControl (modern API) —
        the legacy CheckBoxes.Add collection is unsupported by xlwings.
        """
        cell = sht.range((row, col))
        w = h = 14
        shp = sht.api.Shapes.AddFormControl(
            1,                                       # xlCheckBox
            cell.left + (cell.width - w) / 2,        # centre horizontally
            cell.top + (cell.height - h) / 2,        # centre vertically
            w, h)
        shp.ControlFormat.LinkedCell = cell.address  # "$B$3" — same sheet
        cell.value = bool(checked)                   # authoritative boolean
        cell.number_format = ";;;"                   # hide TRUE/FALSE text
        try:
            shp.TextFrame.Characters().Text = ""     # drop default caption
        except Exception:
            pass

    # ================================================================
    #  Refresh
    # ================================================================

    def refresh_all(self, domains):
        self.store.load()
        self._harvest_notes(list(domains) + ["Selected", "Discarded"])
        restore = self._active_name()
        self.wb.app.screen_updating = False
        try:
            self.write_config_sheet()
            for d in domains: self.write_domain_sheet(d)
            self.write_selected_sheet(); self.write_discarded_sheet()
        finally: self.wb.app.screen_updating = True
        self._activate(restore)

    def _targeted_refresh(self, source, action, affected, all_domains):
        self.store.load()
        dirty = {source}
        if action == "select":  dirty.add("Selected")
        elif action == "discard": dirty.add("Discarded")
        elif action == "restore": dirty.update(affected)
        self._harvest_notes(list(dirty))
        restore = self._active_name()
        self.wb.app.screen_updating = False
        try:
            for name in dirty:
                if name == "Selected":     self.write_selected_sheet()
                elif name == "Discarded":  self.write_discarded_sheet()
                elif name in all_domains:  self.write_domain_sheet(name)
        finally: self.wb.app.screen_updating = True
        self._activate(restore)

    # ================================================================
    #  Actions
    # ================================================================

    def action_on_selection(self, action):
        try:
            sel = self.wb.app.selection
            if sel is None: return
            sht = sel.sheet
        except Exception: return
        if sht.name == "Config": return

        cols = self._columns()
        listing_ids = self._ids_from_selection(sel, sht, self._col_idx(cols, "ID"))
        if not listing_ids: return

        affected, count = set(), 0
        for lid in listing_ids:
            l = self.store.listings.get(lid)
            if not l: continue
            affected.add(l.metadata.source_site)
            if action == "message":
                self._handle_message(lid); count += 1
            else:
                getattr(self.store, f"{action}_listing")(lid); count += 1
        if count:
            self.store.save()
            self._targeted_refresh(sht.name, action, affected,
                                   get_settings().enabled_sites)

    # ================================================================
    #  Config sheet  (Form Control checkboxes for booleans)
    # ================================================================

    def write_config_sheet(self, params=None):
        params = params or get_settings().search
        sht = self._sheet("Config"); sht.clear(); self._clear_shapes(sht)

        all_rows = params.to_excel_rows()
        mid = (len(all_rows) + 1) // 2
        left, right = all_rows[:mid], all_rows[mid:]

        self._add_toolbar(sht, BUTTONS_CONFIG, bg_cols=_CFG_COLS)

        # Left block
        self._cfg_header(sht, HEADER_ROW, 1, 2)
        for i, (lbl, val) in enumerate(left):
            r = DATA_START + i
            sht.range((r, 1)).value = lbl
            if isinstance(val, bool):
                self._add_checkbox(sht, r, 2, val)
            else:
                sht.range((r, 2)).value = val
            self._cfg_row(sht, r, 1, 2, i % 2 == 0)

        # Right block
        self._cfg_header(sht, HEADER_ROW, 4, 5)
        for i, (lbl, val) in enumerate(right):
            r = DATA_START + i
            sht.range((r, 4)).value = lbl
            if isinstance(val, bool):
                self._add_checkbox(sht, r, 5, val)
            else:
                sht.range((r, 5)).value = val
            self._cfg_row(sht, r, 4, 5, i % 2 == 0)

        # Scrape status
        sr = DATA_START + max(len(left), len(right)) + 1
        c = sht.range((sr, 1)); c.value = "Scrape Status:"
        c.font.bold = True; c.font.size = 11
        sht.range((sr, 2)).formula = "=scrape_status()"
        sht.range((sr, 2), (sr, 5)).merge()

        # ── Visible Columns section (checkboxes) ─────────────────
        vc_start = sr + 2
        hdr = sht.range((vc_start, 1), (vc_start, 2))
        hdr.value = [["Visible Columns", "Show"]]
        hdr.font.bold = True; hdr.font.color = CLR_WHITE; hdr.color = CLR_HEADER
        vis = set(get_settings().visible_columns)
        for j, col_name in enumerate(ALL_DATA_COLUMNS):
            r = vc_start + 1 + j
            sht.range((r, 1)).value = col_name
            self._add_checkbox(sht, r, 2, col_name in vis)
            self._cfg_row(sht, r, 1, 2, j % 2 == 0)

        for c_ltr, w in [("A", 24), ("B", 30), ("C", 3), ("D", 24), ("E", 30)]:
            sht.range(f"{c_ltr}:{c_ltr}").column_width = w

    def read_config(self):
        sht = self.wb.sheets["Config"]
        rows = []
        for cl, cv in [(1, 2), (4, 5)]:
            r = DATA_START
            while True:
                label = sht.range((r, cl)).value
                if not label: break
                rows.append((str(label).strip(), sht.range((r, cv)).value))
                r += 1
        return SearchParameters.from_excel_rows(rows)

    def _read_visible_columns(self):
        sht = self.wb.sheets["Config"]
        for r in range(1, 150):
            if sht.range((r, 1)).value == "Visible Columns":
                vis = []
                for j in range(len(ALL_DATA_COLUMNS)):
                    row = r + 1 + j
                    name = sht.range((row, 1)).value
                    show = sht.range((row, 2)).value
                    if name and show is True:
                        vis.append(str(name).strip())
                return vis if vis else list(ALL_DATA_COLUMNS)
        return list(ALL_DATA_COLUMNS)

    def save_config_from_sheet(self):
        try:
            params = self.read_config()
            vis = self._read_visible_columns()
            settings = get_settings()
            settings.search = params; settings.visible_columns = vis
            settings.save()
            import config.settings as _cs; _cs._settings = None
        except Exception as exc:
            logger.warning("Config save failed: %s", exc)

    # ================================================================
    #  Domain / Selected / Discarded
    # ================================================================

    def write_domain_sheet(self, domain):
        sht = self._sheet(domain); sht.clear(); self._clear_shapes(sht)
        cols = self._columns()
        self._add_toolbar(sht, BUTTONS_DOMAIN)
        self._write_header(sht, cols)
        self._flush(sht, cols, *self._build_city_block(
            self.store.by_domain(domain), cols))
        self._finalize(sht, cols)

    def write_all_domain_sheets(self, domains):
        for d in domains: self.write_domain_sheet(d)

    def write_selected_sheet(self):
        self._write_grouped("Selected", self.store.selected_grouped(),
                            BUTTONS_SELECTED)
    def write_discarded_sheet(self):
        self._write_grouped("Discarded", self.store.discarded_grouped(),
                            BUTTONS_DISCARDED)

    def _write_grouped(self, name, grouped, buttons):
        sht = self._sheet(name); sht.clear(); self._clear_shapes(sht)
        cols = self._columns()
        self._add_toolbar(sht, buttons)
        self._write_header(sht, cols)
        self._flush(sht, cols, *self._build_grouped_block(grouped, cols))
        self._finalize(sht, cols)

    # ================================================================
    #  Grid builders
    # ================================================================

    def _build_city_block(self, by_city, cols):
        grid, banners, urls, emails = [], [], [], []
        nc = len(cols)
        for city in sorted(by_city):
            banners.append((len(grid), city, CLR_CITY_BAN))
            grid.append([city] + [None] * (nc - 1))
            for l in by_city[city]:
                idx = len(grid); grid.append(self._to_row(l, cols))
                if l.metadata.source_url: urls.append((idx, l.metadata.source_url))
                if l.email_thread_id: emails.append((idx, l))
        return grid, banners, urls, emails

    def _build_grouped_block(self, grouped, cols):
        grid, banners, urls, emails = [], [], [], []
        nc = len(cols)
        for dom in sorted(grouped):
            banners.append((len(grid), f"● {dom}", CLR_DOMAIN_BAN))
            grid.append([f"● {dom}"] + [None] * (nc - 1))
            for city in sorted(grouped[dom]):
                banners.append((len(grid), f"    {city}", CLR_CITY_BAN))
                grid.append([f"    {city}"] + [None] * (nc - 1))
                for l in grouped[dom][city]:
                    idx = len(grid); grid.append(self._to_row(l, cols))
                    if l.metadata.source_url: urls.append((idx, l.metadata.source_url))
                    if l.email_thread_id: emails.append((idx, l))
        return grid, banners, urls, emails

    def _to_row(self, listing, cols):
        flat = listing.to_excel_row()
        row = []
        for hdr, key in cols:
            val = flat.get(key, "")
            if key == "URL":
                row.append("Link" if val else "")
            elif key == "Email":
                if val and str(val).startswith("pending_"):
                    row.append("Pending")
                else:
                    row.append("Thread" if val else "")
            elif key == "Unread":
                row.append("●" if val else "")
            elif isinstance(val, bool):
                row.append("✓" if val else "")
            else:
                row.append(val)
        return row

    # ════════════════════════════════════════════════════════════════
    #  Bulk writer
    # ════════════════════════════════════════════════════════════════

    def _flush(self, sht, cols, grid, banners, urls, emails):
        if not grid: return
        nc = len(cols); top = DATA_START; bot = top + len(grid) - 1
        sht.range((top, 1), (bot, nc)).value = grid

        for rel, _text, colour in banners:
            r = top + rel; rng = sht.range((r, 1), (r, nc))
            rng.merge(); rng.color = colour; rng.font.color = CLR_WHITE
            rng.font.bold = True; rng.font.size = 12; rng.row_height = 26

        headers = [c[0] for c in cols]
        for ci, hdr in enumerate(headers, 1):
            if hdr in _DATE_NAMES:
                sht.range((top, ci), (bot, ci)).number_format = "yyyy-mm-dd hh:mm"

        url_idx = self._col_idx(cols, "URL")
        email_idx = self._col_idx(cols, "Email")
        if url_idx:
            for rel, url in urls:
                cell = sht.range((top + rel, url_idx))
                try: cell.add_hyperlink(url, text_to_display="Link")
                except Exception: cell.value = url
        if email_idx:
            for rel, l in emails:
                tid = l.email_thread_id or ""
                if tid.startswith("pending_"):
                    continue   # no link for pending inquiries
                cell = sht.range((top + rel, email_idx))
                try:
                    cell.add_hyperlink(
                        self.email.web_url_for(tid),
                        text_to_display="Thread")
                except Exception: pass

    # ════════════════════════════════════════════════════════════════
    #  Notes harvesting
    # ════════════════════════════════════════════════════════════════

    def _harvest_notes(self, sheet_names):
        existing = {s.name for s in self.wb.sheets}
        cols = self._columns()
        id_col = self._col_idx(cols, "ID")
        notes_col = self._col_idx(cols, "Notes")
        if not id_col or not notes_col: return
        nc = len(cols); changed = False
        for name in sheet_names:
            if name not in existing or name == "Config": continue
            sht = self.wb.sheets[name]; last = sht.used_range.last_cell.row
            if last < DATA_START: continue
            raw = sht.range((DATA_START, 1), (last, nc)).value
            if last == DATA_START: raw = [raw]
            for row in raw:
                if not row or not isinstance(row, (list, tuple)): continue
                lid_val = row[id_col - 1]; notes_val = row[notes_col - 1]
                if not lid_val: continue
                lid = str(lid_val).strip()
                notes = str(notes_val).strip() if notes_val else ""
                if lid in self.store.listings:
                    if notes != (self.store.listings[lid].user_notes or ""):
                        self.store.set_notes(lid, notes); changed = True
        if changed: self.store.save()

    # ════════════════════════════════════════════════════════════════
    #  Message handling — on-site inquiry for realtor.ca
    # ════════════════════════════════════════════════════════════════

    def _handle_message(self, listing_id):
        listing = self.store.listings.get(listing_id)
        if not listing: return

        # Already pending or has thread — skip
        if self.threads.is_pending(listing_id):
            logger.info("Inquiry already pending for %s", listing_id)
            return
        tid = listing.email_thread_id or self.threads.get(listing_id)
        if tid and not tid.startswith("pending_"):
            ref = self.email.get_thread(tid)
            if ref:
                self.store.set_email_thread(listing_id, ref.thread_id,
                                            has_unread=ref.has_unread)
            return

        # ── realtor.ca → on-site inquiry form ─────────────────────
        if listing.metadata.source_site == "realtor.ca":
            self._send_realtor_inquiry(listing)
            return

        # ── Other sites → email draft (existing flow) ─────────────
        settings = get_settings().outlook
        to_addr = listing.metadata.contact_email or ""
        subject = settings.default_subject_template.format(address=listing.address)
        body = settings.default_body_template.format(address=listing.address)
        req = DraftRequest(to=to_addr, subject=subject, body=body,
                           listing_id=listing_id)
        try:
            ref = self.email.create_draft(req)
        except NotImplementedError:
            self._open_compose(listing, to_addr, subject, body); return
        self.threads.set(listing_id, ref.thread_id)
        self.store.set_email_thread(listing_id, ref.thread_id,
                                    has_unread=ref.has_unread)

    def _send_realtor_inquiry(self, listing):
        """Launch a visible browser, fill the on-site form, submit."""
        settings = get_settings()
        fn = settings.search.first_name
        ln = settings.search.last_name
        em = settings.search.contact_email
        auto = settings.search.auto_send_messages

        if not em:
            logger.warning("Cannot send inquiry — Contact Email is empty in Config")
            return

        from scrapers.realtor_ca import RealtorCaScraper
        scraper = RealtorCaScraper(headless=False)
        scraper.start()
        try:
            success = scraper.send_inquiry(
                listing, first_name=fn, last_name=ln,
                email=em, auto_send=auto)
            if success:
                self.threads.mark_pending(
                    listing.id,
                    listing.address.full_address,
                    listing.metadata.source_site)
                self.store.set_email_thread(
                    listing.id, f"pending_{listing.id[:8]}", has_unread=False)
                logger.info("Inquiry marked pending for %s", listing.id)
        except Exception as exc:
            logger.error("Inquiry failed for %s: %s", listing.id, exc)
        finally:
            scraper.stop()

    def _open_compose(self, listing, to_addr, subject, body):
        if _HAS_OUTLOOK:
            try:
                ol = win32com.client.Dispatch("Outlook.Application")
                mail = ol.CreateItem(0)
                mail.To = to_addr; mail.Subject = subject; mail.Body = body
                mail.Display(False); return True
            except Exception as exc:
                logger.warning("Outlook compose failed: %s", exc)
        try:
            import webbrowser
            q = urllib.parse.urlencode({"subject": subject, "body": body})
            webbrowser.open(f"mailto:{to_addr}?{q}"); return False
        except Exception: return False

    # ════════════════════════════════════════════════════════════════
    #  Selection → IDs
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _ids_from_selection(sel, sht, id_col):
        if not id_col: return []
        ids = []
        try:
            for area in sel.api.Areas:
                for row_num in range(area.Row, area.Row + area.Rows.Count):
                    if row_num < DATA_START: continue
                    val = sht.range((row_num, id_col)).value
                    if val:
                        lid = str(val).strip()
                        if lid and lid not in ids: ids.append(lid)
        except Exception as exc:
            logger.debug("Selection parse error: %s", exc)
        return ids

    # ════════════════════════════════════════════════════════════════
    #  Toolbar — all buttons same size
    # ════════════════════════════════════════════════════════════════

    def _add_toolbar(self, sht, button_defs, *, bg_cols=None):
        nc = bg_cols or len(self._columns())
        sht.range("1:1").row_height = 42
        sht.range((1, 1), (1, nc)).color = CLR_TOOLBAR_BG
        left = 10.0
        for label, action in button_defs:
            colour = CLR_BTN.get(action, (44, 62, 80))
            shp = sht.api.Shapes.AddShape(
                5, left, _BTN_TOP, _BTN_W, _BTN_H)
            shp.Name = f"btn_{action}"
            shp.Fill.ForeColor.RGB = _rgb(colour)
            shp.Line.Visible = False
            tf = shp.TextFrame2
            tf.TextRange.Text = label
            tf.TextRange.Font.Fill.ForeColor.RGB = _rgb(CLR_WHITE)
            tf.TextRange.Font.Size = 10; tf.TextRange.Font.Bold = True
            tf.TextRange.ParagraphFormat.Alignment = 2  # center
            tf.VerticalAnchor = 3                        # middle
            shp.OnAction = f"RA_{action}"
            left += _BTN_W + _BTN_GAP

    # ════════════════════════════════════════════════════════════════
    #  Low-level helpers
    # ════════════════════════════════════════════════════════════════

    def _sheet(self, name):
        for s in self.wb.sheets:
            if s.name == name: return s
        return self.wb.sheets.add(name, after=self.wb.sheets[-1])

    @staticmethod
    def _clear_shapes(sht):
        try:
            for i in range(sht.api.Shapes.Count, 0, -1):
                sht.api.Shapes.Item(i).Delete()
        except Exception: pass

    def _write_header(self, sht, cols):
        nc = len(cols)
        rng = sht.range((HEADER_ROW, 1), (HEADER_ROW, nc))
        rng.value = [[c[0] for c in cols]]
        rng.font.bold = True; rng.font.color = CLR_WHITE; rng.color = CLR_HEADER

    def _finalize(self, sht, cols):
        try:
            for i, (hdr, _) in enumerate(cols, 1):
                sht.range((HEADER_ROW, i)).column_width = _COL_WIDTHS.get(hdr, 12)
            sht.api.Activate()
            win = sht.book.app.api.ActiveWindow
            win.FreezePanes = False; win.SplitRow = 0; win.SplitColumn = 0
            sht.range((DATA_START, 1)).api.Select(); win.FreezePanes = True
        except Exception: pass

    def _active_name(self):
        try: return self.wb.app.api.ActiveSheet.Name
        except Exception: return None

    def _activate(self, name):
        if name:
            try: self.wb.sheets[name].activate()
            except Exception: pass

    @staticmethod
    def _cfg_header(sht, row, c1, c2):
        for c, txt in ((c1, "Parameter"), (c2, "Value")):
            cell = sht.range((row, c)); cell.value = txt
            cell.font.bold = True; cell.font.color = CLR_WHITE; cell.font.size = 11
        sht.range((row, c1), (row, c2)).color = CLR_HEADER

    @staticmethod
    def _cfg_row(sht, row, c1, c2, alt):
        sht.range((row, c1)).font.bold = True
        sht.range((row, c1)).color = CLR_CFG_ALT_A if alt else CLR_CFG_ALT_B
        sht.range((row, c2)).color = CLR_WHITE
        try:
            border = sht.range((row, c1), (row, c2)).api.Borders(9)
            border.LineStyle = 1; border.Weight = 1
            border.Color = _rgb(CLR_CFG_BORDER)
        except Exception: pass