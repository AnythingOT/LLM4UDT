# Attach_ProcessCSV.py
# Batch UDT generation from a CSV file.
# Each tab (sheet) in a CSV is treated as one UDT — tab name = UDT name.
# Since CSV has no tabs, we support a "UDT_Name" column to group rows.
#
# Expected CSV columns (case-insensitive, order-flexible):
#   UDT_Name   | Tag_Name  | Data_Type | Description
#   MotorStatus| run_status| BOOL      | Motor running flag
#   MotorStatus| speed     | REAL      | Speed setpoint
#   PumpStatus | flow_rate | REAL      | Flow rate
#
# No External_Access column — hardcoded Read/Write per UDT spec.
# Outputs: one .L5X file per UDT in a zip archive.

import csv
import io
import zipfile
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

REQUIRED_COLS = {'tag_name', 'data_type'}
OPTIONAL_COLS = {'udt_name', 'description'}


def _normalise_header(headers: list) -> dict:
    """Map raw CSV header names to canonical keys (case/space insensitive)."""
    mapping = {}
    for i, h in enumerate(headers):
        key = h.strip().lower().replace(' ', '_').replace('-', '_')
        mapping[key] = i
    return mapping


def process_csv_to_udts(csv_bytes: bytes) -> dict:
    """
    Parse CSV bytes and generate one L5X per UDT group.

    Returns:
    {
        "success": bool,
        "udts": [{"udt_name": str, "tag_count": int}],
        "zip_bytes": bytes | None,
        "errors": [str],
        "warnings": [str]
    }

    Nested types are resolved across UDT groups in the same file (a member typed
    as another UDT_Name group is embedded as a nested context definition); a
    non-native type with no matching group errors out for that UDT.
    """
    from Attach_UDTBatch import build_zip_from_specs

    warnings = []

    # ── Parse CSV ─────────────────────────────────────────────────────────────
    try:
        text    = csv_bytes.decode('utf-8-sig')   # strip BOM if present
        reader  = csv.reader(io.StringIO(text))
        rows    = list(reader)
    except Exception as e:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": [f"CSV decode error: {e}"], "warnings": []}

    if len(rows) < 2:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": ["CSV must have a header row and at least one data row."], "warnings": []}

    col_map = _normalise_header(rows[0])

    # Validate required columns
    missing = REQUIRED_COLS - set(col_map.keys())
    if missing:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": [f"Missing required column(s): {', '.join(sorted(missing))}. "
                           f"Expected: Tag_Name, Data_Type (and optionally UDT_Name, Description)."],
                "warnings": []}

    has_udt_col  = 'udt_name'    in col_map
    has_desc_col = 'description' in col_map

    # ── Group rows by UDT name (preserving first-seen order) ──────────────────
    udt_groups: dict[str, list] = defaultdict(list)
    group_order: list = []
    default_udt = "Generated_UDT"

    for row_i, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue  # skip blank rows

        tag_name  = row[col_map['tag_name']].strip()  if col_map.get('tag_name')  is not None and col_map['tag_name']  < len(row) else ''
        data_type = row[col_map['data_type']].strip() if col_map.get('data_type') is not None and col_map['data_type'] < len(row) else ''

        if not tag_name or not data_type:
            warnings.append(f"Row {row_i}: missing Tag_Name or Data_Type — skipped.")
            continue

        udt_name = default_udt
        if has_udt_col and col_map['udt_name'] < len(row):
            raw = row[col_map['udt_name']].strip()
            if raw:
                udt_name = raw

        description = tag_name
        if has_desc_col and col_map['description'] < len(row):
            raw = row[col_map['description']].strip()
            if raw:
                description = raw

        if udt_name not in udt_groups:
            group_order.append(udt_name)
        udt_groups[udt_name].append({
            "name":        tag_name,
            "type":        data_type,
            "description": description,
        })

    if not udt_groups:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": ["No valid data rows found after parsing."], "warnings": warnings}

    # ── Hand the grouped specs to the shared batch builder ────────────────────
    specs  = [(name, udt_groups[name]) for name in group_order]
    result = build_zip_from_specs(specs)
    result["warnings"] = warnings + result.get("warnings", [])
    return result
