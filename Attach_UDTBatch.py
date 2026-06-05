# Attach_UDTBatch.py
# Shared batch-to-zip builder used by BOTH the CSV and Excel importers.
#
# Input: an ordered list of UDT specs — (udt_name, [ {name, type, description}, ... ]).
#   * CSV groups rows by a UDT_Name column.
#   * Excel uses one worksheet per UDT (sheet name = UDT name).
#
# Behaviour:
#   * Each spec becomes one optimised .L5X file in the returned zip.
#   * A member type that is a native base type is used as-is.
#   * A member type that is NOT native is treated as a NESTED UDT: we look for a
#     matching sibling spec in the same batch (case-insensitive). If found, its
#     optimised definition is embedded as a Use="Context" sibling so the file is
#     self-contained. If NOT found anywhere, that UDT errors out (the only error
#     case requested).
#   * Circular nested references are detected and reported (Logix forbids them).

import io
import re
import zipfile
import logging

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 40


def _sanitize_udt_name(name: str) -> str:
    """Mirror Validator_UDT._fix_name so registry names match generated names."""
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", str(name).strip())
    if cleaned and (cleaned[0].isdigit() or cleaned[0] == "_"):
        cleaned = "X_" + cleaned.lstrip("_")
    return cleaned[:MAX_NAME_LENGTH]


def _split_type(fixed_type: str):
    """'Valve[4]' -> ('Valve', 4); 'DINT' -> ('DINT', 0)."""
    m = re.fullmatch(r"([A-Za-z_]\w*)(?:\[(\d+)\])?", fixed_type.strip())
    if not m:
        return fixed_type, 0
    return m.group(1), (int(m.group(2)) if m.group(2) else 0)


def build_zip_from_specs(specs: list) -> dict:
    """
    specs: ordered list of (udt_name, [ {name, type, description}, ... ]).

    Returns:
    {
        "success": bool,
        "udts":    [{"udt_name": str, "tag_count": int}],
        "zip_bytes": bytes | None,
        "errors":  [str],
        "warnings": [str]
    }
    """
    from Validator_UDT import validate_udt
    from L5XOpt_UDT import (optimize_and_regenerate_udt, _topological_sort,
                            _estimate_udt_size, _detect_cycles)

    errors, warnings = [], []

    if not specs:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": ["No UDTs found in the file."], "warnings": []}

    # ── 1. Canonical-name registry (sanitized) ────────────────────────────────
    # Map ORIGINAL upper-case name -> sanitized canonical name, so nested type
    # references (which use original names) resolve to the generated file names.
    name_map: dict = {}
    for raw_name, _ in specs:
        canon = _sanitize_udt_name(raw_name)
        up = str(raw_name).strip().upper()
        if up and canon:
            name_map.setdefault(up, canon)

    # ── 2. Validate each spec (nested types allowed against the registry) ─────
    all_udts: dict = {}     # canonical -> {"name", "members"} for the engine
    spec_order: list = []   # canonical names in original spec order
    seen_canon: set = set()

    for raw_name, raw_tags in specs:
        canonical = name_map.get(str(raw_name).strip().upper()) or _sanitize_udt_name(raw_name)

        vr = validate_udt({"udt_name": raw_name, "tags": raw_tags}, known_udts=name_map)
        if not vr.is_valid:
            errors.append(f"UDT '{canonical}': {'; '.join(vr.errors)}")
            continue
        for w in vr.warnings:
            warnings.append(f"UDT '{canonical}': {w}")

        cname = vr.fixed_data["udt_name"]
        if cname in seen_canon:
            errors.append(f"UDT '{cname}': duplicate UDT name in this file — only the first is kept.")
            continue
        seen_canon.add(cname)

        members = []
        for t in vr.fixed_data["tags"]:
            base, dim = _split_type(t["type"])
            members.append({
                "name":            t["name"],
                "type":            base,
                "dimension":       dim,
                "description":     t.get("description", t["name"]),
                "external_access": t.get("external_access", "Read/Write"),
                "hidden":          False,
            })
        all_udts[cname] = {"name": cname, "members": members}
        spec_order.append(cname)

    if not all_udts:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": errors or ["No valid UDTs could be built."], "warnings": warnings}

    # ── 3. Detect circular nested references ──────────────────────────────────
    dep_graph = {
        name: {m["type"] for m in udt["members"] if m["type"] in all_udts}
        for name, udt in all_udts.items()
    }
    cycles = _detect_cycles(dep_graph)
    if cycles:
        cyclic_names = set()
        for c in cycles:
            cyclic_names.update(p.strip() for p in c.split("→"))
        for c in cycles:
            errors.append(f"Circular nested reference — skipped: {c}")
        # Drop cyclic UDTs so sizing/generation can't recurse infinitely.
        for n in cyclic_names:
            all_udts.pop(n, None)
        spec_order = [n for n in spec_order if n not in cyclic_names]

    if not all_udts:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": errors, "warnings": warnings}

    # ── 4. Size registry in dependency order (leaves first) ───────────────────
    reg: dict = {}
    for n in _topological_sort(all_udts):
        reg[n] = _estimate_udt_size(all_udts[n], reg, {})

    # ── 5. Generate one optimised, self-contained L5X per UDT ─────────────────
    zip_buffer = io.BytesIO()
    generated = []

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for cname in spec_order:
            res = optimize_and_regenerate_udt(
                all_udts[cname], all_udts=all_udts,
                udt_size_registry=reg, aoi_registry={},
                embed_nested_context=True,   # embed nested sibling defs -> self-contained
            )
            if not res.get("success"):
                errors.append(f"UDT '{cname}': generation failed — {res.get('error')}")
                continue
            zf.writestr(f"{cname}.L5X", res["udt_text"])
            generated.append({"udt_name": cname, "tag_count": len(all_udts[cname]["members"])})
            logger.info(f"[Batch] Generated '{cname}' ({len(all_udts[cname]['members'])} members).")

    if not generated:
        return {"success": False, "udts": [], "zip_bytes": None,
                "errors": errors or ["No UDTs could be generated."], "warnings": warnings}

    return {
        "success":   True,
        "udts":      generated,
        "zip_bytes": zip_buffer.getvalue(),
        "errors":    errors,
        "warnings":  warnings,
    }
