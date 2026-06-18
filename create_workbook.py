"""
One-shot workbook initializer.
Embeds the xlwings VBA module directly so the reference is always
available — no manual Tools → References step required.
"""

import sys
from pathlib import Path

import xlwings as xw

from config.settings import get_settings
from excel.interface import ExcelInterface

PROJECT_ROOT = Path(__file__).resolve().parent
UDF_MODULES = "excel.interface"

_ACTIONS = [
    "select", "discard", "restore", "message",
    "refresh", "scrape", "save_config",
]


def _build_vba():
    return "\n\n".join(
        f"Sub RA_{a}()\n"
        f"    RunPython \"import excel.interface as i; "
        f"i.button_action('{a}')\"\nEnd Sub"
        for a in _ACTIONS)


def _inject_vba(wb):
    comp = wb.api.VBProject.VBComponents.Add(1)
    comp.Name = "RA_Macros"
    comp.CodeModule.AddFromString(_build_vba())


def _embed_xlwings_vba(wb):
    """Import the xlwings VBA module so RunPython works out of the box.

    Tries the standalone .bas file first (preferred — no external
    reference needed).  Falls back to adding a reference to the
    xlwings add-in (.xlam) if the .bas file is not found.
    """
    import xlwings
    pkg = Path(xlwings.__path__[0])

    # Standalone: embed the .bas source directly
    bas = pkg / "xlwings.bas"
    if bas.exists():
        wb.api.VBProject.VBComponents.Import(str(bas))
        return

    # Fallback: add a COM reference to the installed xlwings add-in
    for candidate in (pkg / "addin" / "xlwings.xlam",
                      pkg / "xlwings.xlam"):
        if candidate.exists():
            try:
                wb.api.VBProject.References.AddFromFile(str(candidate))
                return
            except Exception:
                continue


def _write_xlwings_conf(wb):
    sht = wb.sheets.add("xlwings.conf", after=wb.sheets[-1])
    sht.range("A1").value = "UDF MODULES"
    sht.range("B1").value = UDF_MODULES
    sht.range("A2").value = "PYTHONPATH"
    sht.range("B2").value = str(PROJECT_ROOT)
    sht.api.Visible = 2   # xlSheetVeryHidden


def create_workbook(path: Path) -> Path:
    abs_path = path.resolve()
    if abs_path.exists():
        raise FileExistsError(f"Refusing to overwrite: {abs_path}")
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    app = xw.App(visible=False, add_book=True)
    try:
        wb = app.books.active
        settings = get_settings()
        initial = ["Config", *settings.enabled_sites, "Selected", "Discarded"]
        wb.sheets[0].name = initial[0]
        for name in initial[1:]:
            wb.sheets.add(name, after=wb.sheets[-1])

        iface = ExcelInterface(workbook=wb)
        iface.write_config_sheet(settings.search)
        iface.write_all_domain_sheets(settings.enabled_sites)
        iface.write_selected_sheet()
        iface.write_discarded_sheet()

        _embed_xlwings_vba(wb)
        _inject_vba(wb)
        _write_xlwings_conf(wb)

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
        print(f"error: {exc}", file=sys.stderr); sys.exit(1)
    print(f"created: {created}")


if __name__ == "__main__":
    main()