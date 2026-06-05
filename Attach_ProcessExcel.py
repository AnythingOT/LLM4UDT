# Attach_ProcessExcel.py
# Batch UDT generation from an Excel (.xlsx / .xlsm) workbook.
#
# Layout convention (one UDT per worksheet):
#   - Each sheet/tab is ONE UDT. The sheet name IS the UDT name.
#   - Column 1 = Tag name, Column 2 = Data type, Column 3 = Description.
#   - An optional header row (e.g. "Tag Name | Data Type | Description") is
#     auto-detected and skipped.
#
# This mirrors Attach_ProcessCSV: every UDT is validated and generated with the
# same engine logic, and all results are returned as one .L5X file per UDT inside
# a single zip archive. External_Access is hardcoded Read/Write per the UDT spec.

import io
import logging

logger = logging.getLogger(__name__)

# Cells whose value matches one of these are treated as a header row, not data.
_HEADER_TAG_WORDS  = {"tag", "tagname", "tag_name", "tag name", "name", "member"}
_HEADER_TYPE_WORDS = {"datatype", "data_type", "data type", "type"}


def _cell(row: tuple, idx: int) -> str:
    """Safe string fetch for a row cell (handles short rows and None)."""
    if idx < len(row) and row[idx] is not None:
        return str(row[idx]).strip()
    return ""


def _looks_like_header(c1: str, c2: str, c3: str) -> bool:
    return (c1.lower() in _HEADER_TAG_WORDS) or (c2.lower() in _HEADER_TYPE_WORDS)


def process_excel_to_udts(xlsx_bytes: bytes) -> dict:
    """
    Parse an Excel workbook and generate one L5X per worksheet (= per UDT).

    Returns (same shape as process_csv_to_udts):
    {
        "success": bool,
        "udts":    [{"udt_name": str, "tag_count": int}],
        "zip_bytes": bytes | None,
        "errors":  [str],
        "warnings": [str]
    }
    """
    import openpyxl
    from Attach_UDTBatch import build_zip_from_specs

    warnings = []

    # ── Open workbook ─────────────────────────────────────────────────────────
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    except Exception as e:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": [f"Could not open Excel workbook: {e}"], "warnings": []}

    if not wb.worksheets:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": ["Workbook contains no worksheets."], "warnings": []}

    # ── Build one spec per sheet (sheet name = UDT name) ──────────────────────
    specs: list = []   # (udt_name, [ {name, type, description}, ... ])

    for ws in wb.worksheets:
        udt_name = (ws.title or "").strip()
        if not udt_name:
            warnings.append("A worksheet has a blank name — skipped.")
            continue

        tags = []
        header_checked = False
        for row in ws.iter_rows(values_only=True):
            if row is None:
                continue
            c1, c2, c3 = _cell(row, 0), _cell(row, 1), _cell(row, 2)

            if not c1 and not c2 and not c3:
                continue  # blank row

            # Skip a single leading header row if present.
            if not header_checked:
                header_checked = True
                if _looks_like_header(c1, c2, c3):
                    continue

            if not c1 or not c2:
                warnings.append(f"Sheet '{udt_name}': row missing Tag name or Data type — skipped.")
                continue

            tags.append({
                "name":        c1,
                "type":        c2,
                "description": c3 if c3 else c1,
            })

        if tags:
            specs.append((udt_name, tags))
        else:
            warnings.append(f"Sheet '{udt_name}': no valid tag rows — skipped.")

    try:
        wb.close()
    except Exception:
        pass

    if not specs:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": ["No worksheets produced any valid UDT rows."], "warnings": warnings}

    # ── Hand the per-sheet specs to the shared batch builder ──────────────────
    result = build_zip_from_specs(specs)
    result["warnings"] = warnings + result.get("warnings", [])
    return result
