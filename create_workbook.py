"""
One-shot initializer for the Rental Aggregator workbook.

• Places the .xlsm at the **project root** (next to this script).
• Injects the ``RAAction`` VBA dispatcher so toolbar buttons work
  immediately via the xlwings add-in's ``RunPython``.
• Creates the ``xlwings.conf`` hidden sheet so "Import Functions"
  on the xlwings ribbon registers the ``scrape_status`` UDF.
• Refuses to overwrite an existing file.
"""

import sys
from pathlib import Path

import xlwings as xw

from config.settings import get_settings
from excel.interface import ExcelInterface

PROJECT_ROOT = Path(__file__).resolve().parent

# Only excel/interface.py exposes UDFs now (just scrape_status).
UDF_MODULES = "excel.interface"

# Single source of truth for action → macro mapping.  Each action gets
# a *parameterless* VBA sub named  RA_<action>  so that a button's
# OnAction is a bare macro name (no argument-string parsing in Excel).
_ACTIONS = [
    "select", "discard", "restore", "message",
    "refresh", "scrape", "save_config",
]


def _build_vba() -> str:
    """One parameterless sub per action, each calling button_action()."""
    subs = []
    for action in _ACTIONS:
        subs.append(
            f"Sub RA_{action}()\n"
            f"    RunPython \"import excel.interface as i; "
            f"i.button_action('{action}')\"\n"
            f"End Sub"
        )
    return "\n\n".join(subs)


def _inject_vba(wb: xw.Book):
    """Add the ``RA_Macros`` module with one sub per action."""
    comp = wb.api.VBProject.VBComponents.Add(1)   # vbext_ct_StdModule
    comp.Name = "RA_Macros"
    comp.CodeModule.AddFromString(_build_vba())


def _write_xlwings_conf(wb: xw.Book):
    """Hidden sheet consumed by the xlwings add-in for UDF registration."""
    sht = wb.sheets.add("xlwings.conf", after=wb.sheets[-1])
    sht.range("A1").value = "UDF MODULES"
    sht.range("B1").value = UDF_MODULES
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

        # Create the initial tab order.
        settings = get_settings()
        initial = [
            "Config",
            *settings.enabled_sites,
            "Selected",
            "Discarded",
        ]
        wb.sheets[0].name = initial[0]
        for name in initial[1:]:
            wb.sheets.add(name, after=wb.sheets[-1])

        # Populate every sheet (store is empty → data sheets are blank).
        iface = ExcelInterface(workbook=wb)
        iface.write_config_sheet(settings.search)
        iface.write_all_domain_sheets(settings.enabled_sites)
        iface.write_selected_sheet()
        iface.write_discarded_sheet()

        # Inject the VBA dispatcher + xlwings UDF config.
        _inject_vba(wb)
        _write_xlwings_conf(wb)

        # FileFormat 52 = xlOpenXMLWorkbookMacroEnabled (.xlsm).
        wb.api.SaveAs(Filename=str(abs_path), FileFormat=52)
        return abs_path
    finally:
        app.quit()


def main():
    settings = get_settings()
    path = PROJECT_ROOT / settings.excel.workbook_name
    try:
        created = create_workbook(path)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"created: {created}")


if __name__ == "__main__":
    main()