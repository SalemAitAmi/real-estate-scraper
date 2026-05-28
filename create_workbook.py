"""
One-shot initializer for the Rental Aggregator workbook.

Refuses to overwrite an existing file. After creation, open the
workbook and click 'Import Functions' on the xlwings ribbon to
register the UDFs from run_scrapers.py and excel/interface.py.
"""

import sys
from pathlib import Path

import xlwings as xw

from config.settings import get_settings
from excel.interface import ExcelInterface


# ── Module list for the xlwings.conf sheet ─────────────────────────
UDF_MODULES = "run_scrapers;excel.interface"


def _write_xlwings_conf(wb):
    """Tell the xlwings add-in which modules to scan for UDFs."""
    sht = wb.sheets.add("xlwings.conf", after=wb.sheets[-1])
    sht.range("A1").value = "UDF MODULES"
    sht.range("B1").value = UDF_MODULES
    # Hide rather than delete so users can edit later if needed.
    sht.api.Visible = 2   # xlSheetVeryHidden


def create_workbook(path: Path) -> Path:
    abs_path = path.resolve()
    if abs_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing workbook: {abs_path}"
        )
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    app = xw.App(visible=False, add_book=True)
    try:
        wb = app.books.active

        initial = ["Config", *get_settings().enabled_sites,
                   "Selected", "Discarded"]
        wb.sheets[0].name = initial[0]
        for name in initial[1:]:
            wb.sheets.add(name, after=wb.sheets[-1])

        iface = ExcelInterface(workbook=wb)
        iface.write_config(get_settings().search)
        iface.write_all_domain_sheets(get_settings().enabled_sites)
        iface.write_selected_sheet()
        iface.write_discarded_sheet()

        _write_xlwings_conf(wb)

        # FileFormat 52 = xlOpenXMLWorkbookMacroEnabled (.xlsm).
        # COM requires an absolute path; relative paths get resolved
        # against Excel's own working directory (My Documents), which
        # is what produced the earlier 41879700 fallback name.
        wb.api.SaveAs(Filename=str(abs_path), FileFormat=52)
        return abs_path
    finally:
        app.quit()


def main():
    settings = get_settings()
    path = Path(settings.excel.data_directory) / settings.excel.workbook_name
    try:
        created = create_workbook(path)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"created: {created}")


if __name__ == "__main__":
    main()