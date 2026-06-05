# Attach_UDTVerify.py
# Analyses a single UDT L5X for sorting and BOOL-packing issues and returns a
# structured report dict for the UI to display.
#
# This module deliberately does NOT keep its own member-ordering table. It
# delegates classification and ordering to the optimizer engine's member_sort_key
# (L5XGen_UDT), so the "expected order" shown in the analysis preview is, by
# construction, identical to the order the optimizer will actually produce.
# The previous local DATA_TYPE_ORDER table disagreed with the engine on arrays,
# LINT/LREAL/COUNTER/CONTROL/MESSAGE, nested UDTs, AOIs, and natural-number sort.

import logging
import xml.etree.ElementTree as ET

from L5XGen_UDT import member_sort_key, SUPPORTED_TYPES
from L5XOpt_UDT import extract_all_udt_definitions

logger = logging.getLogger(__name__)

VALID_BOOL_ARRAY_SIZES = set(range(32, 1025, 32))   # 32, 64 … 1024


def _normalise_for_sort(member: dict) -> dict:
    """
    Map a parsed member into the shape member_sort_key expects.

    Packed scalar BOOLs are stored as DataType="BIT" in an export; the optimizer
    treats BIT as BOOL before sorting, so we mirror that here. Everything else is
    passed through unchanged (registry membership is checked on the raw type).
    """
    t = member.get("type", "")
    if t.upper() == "BIT":
        return {**member, "type": "BOOL"}
    return member


def _category_label(member: dict, udt_registry: dict, aoi_registry: dict) -> str:
    """
    Human-readable category for the UI, derived from the SAME group decision
    member_sort_key uses — so the label always agrees with the sort position.
    """
    m    = _normalise_for_sort(member)
    base = m.get("type", "").upper()
    dim  = m.get("dimension", 0) or 0

    is_native = base in SUPPORTED_TYPES
    is_udt    = (not is_native) and (m["type"] in (udt_registry or {}))
    is_aoi    = (not is_native) and (not is_udt) and (m["type"] in (aoi_registry or {}))

    if dim > 0:
        if is_udt:  return f"UDT {m['type']} array"
        if is_aoi:  return f"AOI {m['type']} array"
        if is_native: return f"{base} array"
        return f"{m['type']} array"

    if is_aoi:    return f"AOI ({m['type']})"
    if is_udt:    return f"UDT ({m['type']})"
    if base == "BOOL":
        return "BOOL (scalar)"
    if is_native: return base
    return f"Other ({m['type']})"


def analyse_udt(l5x_content: str) -> dict:
    """
    Parse a single-UDT L5X string and return a structured analysis report whose
    "expected" ordering matches the optimizer engine exactly.

    Returns:
    {
        "udt_name": str,
        "member_count": int,
        "issues": [
            {"type": "sort_order"|"bool_packing", "severity": "warning"|"error", "detail": str}
        ],
        "members": [
            {"name", "type", "dimension", "category",
             "position", "expected_position", "out_of_order"}
        ],
        "needs_optimization": bool,
        "summary": str
    }
    or {"error": str} on failure.
    """
    parsed = extract_all_udt_definitions(l5x_content)
    if isinstance(parsed, dict) and parsed.get("error"):
        return {"error": parsed["error"]}

    target   = parsed.get("target")
    all_udts = parsed.get("udts", {})
    aoi_reg  = parsed.get("aoi_registry", {})

    if not target or target not in all_udts:
        if len(all_udts) == 1:
            target = next(iter(all_udts))
        else:
            return {"error": "No target DataType element found."}

    udt_name = target
    # _parse_members already drops hidden ZZZZ backing SINTs and keeps the
    # logical members (BIT bits included) — exactly the set we want to order.
    visible  = all_udts[target].get("members", [])

    # udt_registry only needs membership, so the all_udts dict (keyed by name)
    # is a valid registry for member_sort_key.
    udt_reg = all_udts

    def _key(m):
        return member_sort_key(_normalise_for_sort(m), udt_reg, aoi_reg)

    current_names = [m.get("name", "") for m in visible]
    optimal       = sorted(visible, key=_key)
    optimal_names = [m.get("name", "") for m in optimal]

    issues  = []
    members = []

    # ── 1. Sort-order check (engine-accurate) ─────────────────────────────────
    if current_names != optimal_names:
        for i, (cur, opt) in enumerate(zip(current_names, optimal_names)):
            if cur != opt:
                issues.append({
                    "type": "sort_order",
                    "severity": "warning",
                    "detail": f"Incorrect member order — '{cur}' at position {i+1}, expected '{opt}'.",
                })
                break

    # ── 2. Unpacked scalar BOOL check ─────────────────────────────────────────
    # A scalar BOOL stored as DataType="BOOL" (not BIT) is an un-packed bit and
    # can be folded into a SINT backing field; BIT members are already packed.
    bool_scalars = [m for m in visible
                    if m.get("type", "").upper() == "BOOL" and (m.get("dimension", 0) or 0) == 0]
    if bool_scalars:
        names = ", ".join(m.get("name", "?") for m in bool_scalars)
        issues.append({
            "type": "bool_packing",
            "severity": "warning",
            "detail": f"Scalar BOOL not bit-packed (stored as BOOL, should be BIT): {names}.",
        })

    # ── 3. BOOL array size check ──────────────────────────────────────────────
    for m in visible:
        if m.get("type", "").upper() == "BOOL":
            dim = m.get("dimension", 0) or 0
            if dim > 0 and dim not in VALID_BOOL_ARRAY_SIZES:
                issues.append({
                    "type": "bool_packing",
                    "severity": "error",
                    "detail": f"'{m.get('name')}' is BOOL[{dim}] — size must be a multiple of 32 (32–1024).",
                })

    # ── 3b. Unresolved-type check ─────────────────────────────────────────────
    # Mirrors the optimizer: a member whose type is not native, not a known UDT
    # and not an AOI cannot be sized/optimised here. It is preserved (never
    # dropped), but flagged so the user knows its definition is missing from this
    # file (typically resolved by uploading the full program export).
    unresolved = []
    for m in visible:
        base = m.get("type", "").upper()
        if base == "BIT":
            continue
        if base in SUPPORTED_TYPES:
            continue
        if m.get("type") in udt_reg or m.get("type") in aoi_reg:
            continue
        unresolved.append((m.get("name", "?"), m.get("type", "?")))
    if unresolved:
        names = ", ".join(f"{n} ({t})" for n, t in unresolved)
        issues.append({
            "type": "unresolved_type",
            "severity": "warning",
            "detail": (f"Unresolved type(s) — preserved unchanged, not optimised: {names}. "
                       f"Upload the full program L5X to resolve these."),
        })

    # ── 4. Per-member detail with engine-accurate expected position ───────────
    name_to_expected = {name: i for i, name in enumerate(optimal_names)}
    for i, m in enumerate(visible):
        name = m.get("name", "")
        exp  = name_to_expected.get(name, i)
        members.append({
            "name":              name,
            "type":              m.get("type", ""),
            "dimension":         m.get("dimension", 0) or 0,
            "category":          _category_label(m, udt_reg, aoi_reg),
            "position":          i,
            "expected_position": exp,
            "out_of_order":      i != exp,
        })

    needs_opt = bool(issues)
    summary = (f"{len(issues)} issue(s) found — optimization recommended."
               if needs_opt else "UDT is already optimized. No issues found.")

    return {
        "udt_name":           udt_name,
        "member_count":       len(visible),
        "issues":             issues,
        "members":            members,
        "needs_optimization": needs_opt,
        "summary":            summary,
    }
