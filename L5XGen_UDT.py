# L5XGen_UDT.py
import datetime
import re

# ---------------------------------------------------------------------------
# Byte sizes for known native types
# ---------------------------------------------------------------------------
TYPE_SIZES = {
    "BOOL":    1,
    "SINT":    1,
    "INT":     2,
    "DINT":    4,
    "LINT":    8,
    "REAL":    4,
    "LREAL":   8,
    "STRING":  88,
    "TIMER":   12,
    "COUNTER": 12,
    "CONTROL": 12,
    "MESSAGE": 50,
}

# ---------------------------------------------------------------------------
# Sort groups
#   0  BOOL scalar
#   1  SINT scalar
#   2  INT  scalar
#   3  DINT scalar
#   4  ALL arrays
#   5  REAL scalar
#   6  LREAL scalar
#   7  LINT scalar
#   8  STRING scalar
#   9  TIMER/COUNTER/CTRL/AOI
#  10  Nested UDT scalars
#  11  Other/unknown
# ---------------------------------------------------------------------------
SCALAR_GROUP = {
    "BOOL":    0,
    "SINT":    1,
    "INT":     2,
    "DINT":    3,
    "REAL":    5,
    "LREAL":   6,
    "LINT":    7,
    "STRING":  8,
    "TIMER":   9,
    "COUNTER": 9,
    "CONTROL": 9,
    "MESSAGE": 9,
}

ARRAY_SUBGROUP = {
    "BOOL":    0,
    "SINT":    1,
    "INT":     2,
    "DINT":    3,
    "REAL":    4,
    "LREAL":   5,
    "LINT":    6,
    "STRING":  7,
    "TIMER":   8,
    "COUNTER": 8,
    "CONTROL": 8,
    "MESSAGE": 8,
}

SUPPORTED_TYPES = {
    "BOOL", "SINT", "INT", "DINT", "LINT",
    "REAL", "LREAL", "STRING",
    "TIMER", "COUNTER", "CONTROL", "MESSAGE",
}


def natural_key(name: str) -> list:
    parts = re.split(r'(\d+)', name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())


def member_sort_key(member: dict, udt_registry: dict = None, aoi_registry: dict = None,
                    string_family_names: set = None) -> tuple:
    """
    AOI types sort with group 9 (TIMER/COUNTER group).
    String-family user types (Family="StringFamily" UDTs like STRING_40) sort
    with native STRING (group 8 scalar / subgroup 7 array) so all string-like
    members are laid out together for better alignment grouping.

    Note: the byte size of the built-in STRING type is correctly 88 (4 LEN + 82
    DATA + 2 pad) in TYPE_SIZES. User-defined STRING_N types are sized
    dynamically via the UDT registry from their actual LEN/DATA members — they
    do NOT use the 88-byte literal.
    """
    base_type = member["type"].upper()
    is_array  = member.get("dimension", 0) > 0
    is_native = base_type in SUPPORTED_TYPES
    is_udt    = (not is_native) and (udt_registry and member["type"] in udt_registry)
    is_aoi    = (not is_native) and (not is_udt) and (aoi_registry and member["type"] in aoi_registry)
    is_strfam = (not is_native) and is_udt and (string_family_names and member["type"] in string_family_names)
    nk        = natural_key(member["name"])

    if is_array:
        if is_strfam:
            return (4, ARRAY_SUBGROUP["STRING"], nk)
        sub = ARRAY_SUBGROUP.get(base_type, 9 if (is_udt or is_aoi) else 10)
        return (4, sub, nk)

    if is_aoi:
        return (9, 0, nk)

    if is_strfam:
        return (SCALAR_GROUP["STRING"], 0, nk)

    grp = SCALAR_GROUP.get(base_type, 10 if is_udt else 11)
    return (grp, 0, nk)


def format_description(description: str) -> str:
    if description and description.strip():
        return (
            f"\n          <Description>\n"
            f"            <![CDATA[{description.strip()}]]>\n"
            f"          </Description>"
        )
    return ""


def generate_udt_l5x_from_tags(data_dict: dict, controller_name: str = "Controller",
                                sw_revision: str = "32.04",
                                udt_registry: dict = None,
                                aoi_registry: dict = None,
                                dependencies_xml: str = None,
                                aoi_context_xml: str = None,
                                nested_context_xml: str = None) -> dict:
    """
    Generate a single-UDT L5X from a tag list.

    Args:
        data_dict:       {"udt_name": str, "tags": [...]}
        controller_name: Name attribute for <Controller Use="Context">
        udt_registry:    {name: size_bytes} for nested UDT resolution
        aoi_registry:    {name: size_bytes} for AOI type resolution
        dependencies_xml: Raw <Dependencies>...</Dependencies> XML string to inject
                          verbatim inside <DataType> after </Members>. Carries the
                          Rockwell dependency declarations required for Studio 5000
                          to resolve AOI references on import.
        aoi_context_xml: Raw <AddOnInstructionDefinitions Use="Context">...</>
                          XML string to inject verbatim inside <Controller> after
                          </DataTypes>. Carries the full AOI definition so Studio 5000
                          can validate the member type on import.
        nested_context_xml: Raw concatenation of nested <DataType Use="Context">...
                          </DataType> blocks to inject verbatim inside <DataTypes>
                          after the target </DataType>. Carries the definitions of
                          any nested UDTs the target references so the single-UDT
                          export is self-contained and resolves on import.
    """
    if udt_registry  is None: udt_registry  = {}
    if aoi_registry  is None: aoi_registry  = {}

    if not isinstance(data_dict, dict):
        return {"success": False, "error": "Input must be a dictionary."}

    tag_list = data_dict.get("tags", [])
    udt_name = sanitize_name(data_dict.get("udt_name", "GeneratedUDT"))

    if not isinstance(tag_list, list) or not tag_list:
        return {"success": False, "error": "No tags provided."}

    try:
        now = datetime.datetime.utcnow().strftime('%a %b %d %H:%M:%S %Y')
        cleaned = []

        for tag in tag_list:
            raw_type = str(tag.get("type", "")).strip()
            match = re.match(r"([A-Za-z_]\w*)(?:\[(\d*)\])?$", raw_type, re.IGNORECASE)
            if not match:
                continue

            base_raw, size_str = match.groups()
            base_upper = base_raw.upper()

            if size_str is None:
                dimension = 0
            elif size_str == "":
                dimension = 1
            else:
                try:
                    dimension = max(0, int(size_str))
                except ValueError:
                    continue

            is_native = base_upper in SUPPORTED_TYPES
            is_nested = (not is_native) and (base_raw in udt_registry)
            is_aoi    = (not is_native) and (not is_nested) and (base_raw in aoi_registry)
            # An unresolved type (not native, not a known UDT, not an AOI) is most
            # likely a structured type whose definition lives elsewhere (e.g. a
            # Rockwell library AOI/UDT not included in this export). We CANNOT
            # optimise around it (unknown size/alignment), but dropping it would
            # silently delete the user's field — so we carry it through verbatim.
            is_unknown = (not is_native) and (not is_nested) and (not is_aoi)

            base_type = base_upper if is_native else base_raw

            # Rockwell BOOL array: dimension must be multiple of 32. An invalid
            # BOOL[N] cannot be represented in L5X at all, so it is skipped (this
            # only triggers on already-malformed input Studio 5000 would reject).
            if base_type == "BOOL" and dimension > 0 and dimension % 32 != 0:
                continue

            name = sanitize_name(str(tag.get("name", "")).strip())
            if not name:
                continue

            cleaned.append({
                "name":            name,
                "type":            base_type,
                "dimension":       dimension,
                "description":     str(tag.get("description", name)),
                "external_access": tag.get("external_access", "Read/Write"),
                "hidden":          tag.get("hidden", False),
                # NullType radix for any structured/complex/unknown type.
                "is_nested_udt":   is_nested or is_aoi or is_unknown,
                "is_aoi":          is_aoi,
            })

        if not cleaned:
            return {"success": False, "error": "No valid or supported tags found."}

        sorted_tags = sorted(
            cleaned,
            key=lambda m: member_sort_key(m, udt_registry, aoi_registry)
        )

        # Build XML members with BOOL bit-packing
        l5x_members = []
        bool_pack   = []

        def flush_bool_pack():
            if not bool_pack:
                return
            sint_name = f"ZZZZZZZZZZ{bool_pack[0]['name']}"
            l5x_members.append(
                f'          <Member Name="{sint_name}" DataType="SINT" '
                f'Dimension="0" Radix="Decimal" Hidden="true" ExternalAccess="Read/Write"/>'
            )
            for bit_idx, t in enumerate(bool_pack):
                desc = format_description(t["description"])
                l5x_members.append(
                    f'          <Member Name="{t["name"]}" DataType="BIT" '
                    f'Dimension="0" Radix="Decimal" Hidden="false" '
                    f'Target="{sint_name}" BitNumber="{bit_idx}" '
                    f'ExternalAccess="{t["external_access"]}">'
                    f'{desc}\n          </Member>'
                )
            bool_pack.clear()

        for tag in sorted_tags:
            dtype, dimension = tag["type"], tag["dimension"]

            if dtype == "BOOL" and dimension == 0:
                bool_pack.append(tag)
                if len(bool_pack) == 8:
                    flush_bool_pack()
                continue

            flush_bool_pack()

            is_complex = tag["is_nested_udt"]
            if is_complex or dtype in ("STRING", "TIMER", "COUNTER", "CONTROL", "MESSAGE"):
                radix = "NullType"
            elif dtype in ("REAL", "LREAL"):
                radix = "Float"
            else:
                radix = "Decimal"

            desc       = format_description(tag["description"])
            hidden_str = str(tag["hidden"]).lower()
            l5x_members.append(
                f'          <Member Name="{tag["name"]}" DataType="{dtype}" '
                f'Dimension="{dimension}" Radix="{radix}" '
                f'Hidden="{hidden_str}" ExternalAccess="{tag["external_access"]}">'
                f'{desc}\n          </Member>'
            )

        flush_bool_pack()

        members_xml = "\n".join(l5x_members)

        # Build <Dependencies> block — injected verbatim, matching Rockwell compact style
        deps_block = ""
        if dependencies_xml and dependencies_xml.strip():
            # Strip existing indentation and re-apply none — Rockwell uses no indentation
            # inside single-UDT exports for Dependencies
            deps_block = f"\n{dependencies_xml.strip()}"

        # Build <AddOnInstructionDefinitions Use="Context"> block
        # Injected verbatim — preserves CDATA, original Rockwell formatting
        aoi_ctx_block = ""
        if aoi_context_xml and aoi_context_xml.strip():
            aoi_ctx_block = f"\n{aoi_context_xml.strip()}"

        # Build nested UDT context block — the <DataType Use="Context"> definitions
        # of any nested UDTs the target references. Injected verbatim as siblings of
        # the target DataType so the single-UDT export is self-contained and resolves
        # on import (matching Studio 5000's own single-UDT export behaviour).
        nested_ctx_block = ""
        if nested_context_xml and nested_context_xml.strip():
            nested_ctx_block = f"\n{nested_context_xml.strip()}"

        udt_l5x = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="{sw_revision}" '
            f'TargetName="{udt_name}" TargetType="DataType" ContainsContext="true" '
            f'ExportDate="{now}" ExportOptions="References NoRawData L5KData '
            f'DecoratedData Context Dependencies ForceProtectedEncoding AllProjDocTrans">\n'
            f'  <Controller Use="Context" Name="{controller_name}">\n'
            f'    <DataTypes Use="Context">\n'
            f'      <DataType Use="Target" Name="{udt_name}" Family="NoFamily" Class="User">\n'
            f'        <Members>\n'
            f'{members_xml}\n'
            f'        </Members>{deps_block}\n'
            f'      </DataType>{nested_ctx_block}\n'
            f'    </DataTypes>{aoi_ctx_block}\n'
            f'  </Controller>\n'
            f'</RSLogix5000Content>'
        )

        # Normalise line endings: the wrapper/target are generated with \n while
        # injected verbatim blocks (Dependencies, nested context) may carry \r\n.
        # Unify to CRLF to match Studio 5000's own export style and avoid a file
        # with mixed endings. (No BOM here — this string is also re-parsed by ET
        # in the full-program rebuild, where a leading BOM would break parsing.)
        udt_l5x = re.sub(r'\r\n|\r|\n', '\r\n', udt_l5x)

        return {
            "success":       True,
            "udt_text":      udt_l5x,
            "download_name": f"{udt_name}.l5x",
            "message":       "OK",
        }

    except Exception as e:
        return {"success": False, "error": f"Exception: {e}"}
