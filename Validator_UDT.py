# Validator_UDT.py
#
# Validates and auto-fixes a UDT tag dict BEFORE L5X generation.
# Rules enforced:
#   1. UDT name must not start with a digit or special character
#   2. UDT name must only contain letters, digits, underscores (max 40 chars)
#   3. Member names must not start with a digit or special character
#   4. Member names must only contain letters, digits, underscores (max 40 chars)
#   5. Scalar BOOLs must be packed as BIT members (no stray BOOL[1] or bare BOOL arrays < 32)
#   6. BOOL arrays must be multiples of 32 (32, 64 … 1024)
#   7. Supported types only: BOOL, SINT, INT, DINT, REAL, STRING, TIMER, COUNTER (+ arrays)
#   8. No duplicate member names (case-insensitive)
#   9. Dimension must be a non-negative integer
#  10. Members list must not be empty after all fixes
#
# Returns a ValidationResult with:
#   - is_valid: bool              — False means generation should be blocked
#   - fixed_data: dict            — corrected copy of the input dict (best-effort)
#   - errors: list[str]           — blocking issues (not auto-fixable)
#   - warnings: list[str]         — auto-fixed issues (informational)

import re
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

SUPPORTED_BASE_TYPES = {"BOOL", "SINT", "INT", "DINT", "REAL", "STRING", "TIMER", "COUNTER"}
MAX_NAME_LENGTH = 40
VALID_BOOL_ARRAY_SIZES = set(range(32, 1025, 32))   # 32, 64, 96 … 1024


@dataclass
class ValidationResult:
    is_valid: bool = True
    fixed_data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str):
        self.errors.append(msg)
        self.is_valid = False

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def summary(self) -> str:
        lines = []
        if self.warnings:
            lines.append("Auto-fixed:")
            lines += [f"  ⚠ {w}" for w in self.warnings]
        if self.errors:
            lines.append("Blocking errors:")
            lines += [f"  ✗ {e}" for e in self.errors]
        if not lines:
            lines.append("✓ All validation checks passed.")
        return "\n".join(lines)


def _fix_name(name: str, context: str, result: ValidationResult) -> str:
    """
    Sanitize a name to only letters, digits, underscores.
    Prefix with 'X_' if it starts with a digit or underscore.
    Truncate to MAX_NAME_LENGTH.
    """
    original = name
    # Replace invalid chars with underscore
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    # Must not start with digit or underscore
    if cleaned and (cleaned[0].isdigit() or cleaned[0] == "_"):
        cleaned = "X_" + cleaned.lstrip("_")
    # Truncate
    if len(cleaned) > MAX_NAME_LENGTH:
        cleaned = cleaned[:MAX_NAME_LENGTH]
    if cleaned != original:
        result.add_warning(f"{context} name '{original}' → '{cleaned}' (sanitized)")
    if not cleaned:
        result.add_error(f"{context} produced an empty name after sanitization (original: '{original}')")
    return cleaned


def _parse_type(raw_type: str):
    """
    Parse 'TYPE' or 'TYPE[N]' into (base_type, dimension).

    base_type is returned in its ORIGINAL case (callers uppercase it to test for
    native base types; nested UDT type names are case-sensitive and must keep
    their original casing to match a sibling UDT). The base accepts any valid
    Logix identifier (letters, digits, underscores), not just A–Z, so nested
    type names like 'UDT_Unsorted' or 'raC_UDT_Stratix' parse correctly.
    Returns (None, None) on failure.
    """
    raw = raw_type.strip()
    m = re.fullmatch(r"([A-Za-z_]\w*)(?:\[(\d+)\])?", raw)
    if not m:
        return None, None
    base = m.group(1)
    dim = int(m.group(2)) if m.group(2) is not None else 0
    return base, dim


def validate_udt(data: dict, known_udts=None) -> ValidationResult:
    """
    Main entry point. Takes the same dict that goes into generate_udt_l5x_from_tags:
        { "udt_name": str, "tags": [ {name, type, description?, ...}, ... ] }
    Returns a ValidationResult with a corrected fixed_data copy.

    known_udts (optional): the set of sibling UDT names available in the same
    batch (e.g. other sheets in a workbook or other UDT groups in a CSV). A
    member whose type is not a native base type but DOES name one of these is
    accepted as a nested UDT reference and resolved to that sibling's exact
    (sanitized) name. A non-native type that is neither a base type nor a known
    sibling is the ONLY case that raises an error. Accepts either a mapping
    {UPPER_NAME: canonical_name} or an iterable of canonical names.
    """
    result = ValidationResult()

    # Normalise known_udts into {UPPER: canonical}
    if isinstance(known_udts, dict):
        known_map = {k.upper(): v for k, v in known_udts.items()}
    elif known_udts:
        known_map = {str(n).upper(): str(n) for n in known_udts}
    else:
        known_map = {}

    if not isinstance(data, dict):
        result.add_error("Input is not a dictionary.")
        return result

    fixed = {"udt_name": "", "tags": []}

    # ── 1-2. UDT name ─────────────────────────────────────────────────────────
    raw_udt_name = str(data.get("udt_name", "Generated_UDT")).strip()
    fixed["udt_name"] = _fix_name(raw_udt_name, "UDT", result)

    # ── Tags list ─────────────────────────────────────────────────────────────
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not tags:
        result.add_error("'tags' must be a non-empty list.")
        return result

    seen_names: set = set()
    fixed_tags = []

    for i, tag in enumerate(tags):
        tag_ctx = f"Tag[{i}]"
        if not isinstance(tag, dict):
            result.add_warning(f"{tag_ctx} is not a dict — skipped.")
            continue

        # ── 3-4. Member name ──────────────────────────────────────────────────
        raw_name = str(tag.get("name", "")).strip()
        if not raw_name:
            result.add_warning(f"{tag_ctx} has no name — skipped.")
            continue
        name = _fix_name(raw_name, f"Member '{raw_name}'", result)
        if not name:
            continue  # error already logged

        # ── 8. Duplicate names ────────────────────────────────────────────────
        key = name.lower()
        if key in seen_names:
            result.add_error(f"Duplicate member name '{name}' (case-insensitive) — skipped.")
            continue
        seen_names.add(key)

        # ── 7 + 9. Type parsing ───────────────────────────────────────────────
        raw_type = str(tag.get("type", "")).strip()
        base_type, dimension = _parse_type(raw_type)

        if base_type is None:
            result.add_error(f"Member '{name}': unrecognised type format '{raw_type}' — skipped.")
            continue

        base_upper = base_type.upper()
        is_native  = base_upper in SUPPORTED_BASE_TYPES
        is_nested  = (not is_native) and (base_upper in known_map)

        if not is_native and not is_nested:
            # The only error case the user asked for: a nested/structured type
            # whose definition isn't available anywhere in this batch.
            result.add_error(
                f"Member '{name}': type '{base_type}' is not a base type and no "
                f"matching UDT was found in this file — cannot resolve nested type."
            )
            continue

        if dimension < 0:
            result.add_error(f"Member '{name}': negative dimension {dimension} — skipped.")
            continue

        if is_native:
            # ── 5. BOOL[1] ambiguity ─────────────────────────────────────────
            if base_upper == "BOOL" and dimension == 1:
                result.add_warning(
                    f"Member '{name}': BOOL[1] is ambiguous — treated as scalar BOOL (dimension set to 0)."
                )
                dimension = 0

            # ── 6. BOOL array size must be multiple of 32 ────────────────────
            if base_upper == "BOOL" and dimension > 0 and dimension not in VALID_BOOL_ARRAY_SIZES:
                if dimension > 1024:
                    result.add_error(
                        f"Member '{name}': BOOL[{dimension}] exceeds the maximum "
                        f"BOOL array size of 1024 — split into multiple members."
                    )
                    continue
                # Round UP to the next multiple of 32 so the user never silently
                # loses bits. Rounding to nearest could shrink BOOL[33]→BOOL[32]
                # and drop the 33rd bit the user explicitly asked for.
                rounded = ((dimension + 31) // 32) * 32
                result.add_warning(
                    f"Member '{name}': BOOL[{dimension}] is not a valid size. "
                    f"Rounded UP to BOOL[{rounded}] (must be a multiple of 32, max 1024)."
                )
                dimension = rounded

            resolved_type = base_upper
        else:
            # Nested UDT — resolve to the sibling's exact (sanitized) name.
            resolved_type = known_map[base_upper]

        # Reconstruct type string
        fixed_type = f"{resolved_type}[{dimension}]" if dimension > 0 else resolved_type

        fixed_tag = {
            "name": name,
            "type": fixed_type,
            "description": str(tag.get("description", name)).strip() or name,
            "external_access": tag.get("external_access", "Read/Write"),
            "hidden": bool(tag.get("hidden", False)),
        }
        fixed_tags.append(fixed_tag)

    # ── 10. At least one member must survive ──────────────────────────────────
    if not fixed_tags:
        result.add_error("No valid members remain after validation — cannot generate UDT.")

    fixed["tags"] = fixed_tags
    result.fixed_data = fixed
    return result
