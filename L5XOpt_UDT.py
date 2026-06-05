# L5XOpt_UDT.py
import logging
import xml.etree.ElementTree as ET
import re
from L5XGen_UDT import generate_udt_l5x_from_tags, SUPPORTED_TYPES, TYPE_SIZES

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_cycles(graph: dict) -> list:
    """DFS cycle detection. Returns list of cycle description strings."""
    visited, rec_stack, cycles = set(), set(), []

    def dfs(node, path):
        visited.add(node); rec_stack.add(node)
        for nb in graph.get(node, set()):
            if nb not in visited:
                dfs(nb, path + [nb])
            elif nb in rec_stack:
                idx = path.index(nb) if nb in path else 0
                cycles.append(" → ".join(path[idx:] + [nb]))
        rec_stack.discard(node)

    for node in list(graph):
        if node not in visited:
            dfs(node, [node])
    return cycles


def _topological_sort(all_udts: dict) -> list:
    """Return UDT names in dependency order (leaves first)."""
    dep_graph = {
        name: {m["type"] for m in udt.get("members", []) if m["type"] in all_udts}
        for name, udt in all_udts.items()
    }
    order, visited = [], set()

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        for dep in dep_graph.get(node, set()):
            visit(dep)
        order.append(node)

    for name in all_udts:
        visit(name)
    return order


def _align(offset: int, alignment: int) -> int:
    """Round offset up to the next multiple of alignment."""
    return (offset + alignment - 1) & ~(alignment - 1)


_TYPE_ALIGNMENT = {
    "BOOL":    1,
    "SINT":    1,
    "INT":     2,
    "DINT":    4,
    "LINT":    8,
    "REAL":    4,
    "LREAL":   8,
    "STRING":  4,
    "TIMER":   4,
    "COUNTER": 4,
    "CONTROL": 4,
    "MESSAGE": 2,
}


def _estimate_aoi_size(aoi_element) -> int:
    """
    Estimate in-memory size of an AOI instance from its Parameter list.
    Parameters are laid out as a struct in declaration order.
    """
    members = []
    for p in aoi_element.findall("./Parameters/Parameter"):
        dtype = p.get("DataType", "DINT")
        members.append({"type": dtype, "dimension": 0})
    return _estimate_udt_size({"members": members}, {})


def _estimate_udt_size(udt: dict, registry: dict, aoi_registry: dict = None) -> int:
    """
    Rockwell-accurate byte-size estimate (verified against Studio 5000).

    Layout rules:
    - Consecutive BIT members → packed into SINT(s), SINT block 4B-aligned.
    - BOOL[N] arrays → stored as DINT array (N/32 × 4B), 4B-aligned.
    - AOI instances → sized from their parameter list.
    - All other types → natural alignment per _TYPE_ALIGNMENT.
    - Final total padded to 4B minimum.
    """
    if aoi_registry is None:
        aoi_registry = {}

    members   = udt.get("members", [])
    offset    = 0
    max_align = 4
    i = 0

    while i < len(members):
        m     = members[i]
        mtype = m.get("type", "").upper()
        raw   = m.get("type", "")
        dim   = m.get("dimension", 0)

        if mtype == "BIT":
            bits = 0
            while i < len(members) and members[i].get("type", "").upper() == "BIT":
                bits += 1
                i    += 1
            n_bytes = (bits + 7) // 8
            offset  = _align(offset, 4)
            offset += n_bytes
            continue

        elif mtype == "BOOL" and dim > 0:
            sz  = ((dim + 31) // 32) * 4
            aln = 4

        elif mtype == "BOOL" and dim == 0:
            sz  = 1
            aln = 1

        elif mtype in TYPE_SIZES:
            sz  = TYPE_SIZES[mtype] * max(dim, 1)
            aln = _TYPE_ALIGNMENT.get(mtype, 1)

        elif raw in registry:
            sz  = registry[raw] * max(dim, 1)
            aln = min(registry[raw], 4)

        elif raw in aoi_registry:
            sz  = aoi_registry[raw] * max(dim, 1)
            aln = min(aoi_registry[raw], 4)

        else:
            sz  = 4 * max(dim, 1)
            aln = 4

        max_align = max(max_align, aln)
        offset    = _align(offset, aln)
        offset   += sz
        i        += 1

    return _align(offset, max_align)


def _compute_member_list_size(members: list, registry: dict, aoi_registry: dict = None) -> int:
    """Compute aligned size from a flat list of {type, dimension} dicts."""
    return _estimate_udt_size({"members": members}, registry, aoi_registry or {})


# ---------------------------------------------------------------------------
# L5X detection
# ---------------------------------------------------------------------------

def detect_l5x_type(l5x_content: str) -> str:
    try:
        root = ET.fromstring(l5x_content)
        tt = root.get("TargetType", "")
        if tt == "DataType":   return "single_udt"
        if tt == "Controller": return "full_program"
        return "unknown"
    except ET.ParseError:
        return "unknown"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_members(dt_element) -> list:
    """
    Extract members from a <DataType> element.
    Skips hidden ZZZZZZZZZZ padding members (regenerated during optimisation).
    """
    members = []
    for m in dt_element.findall("./Members/Member"):
        mname = m.get("Name", "")
        if not mname or mname.startswith("ZZZZZZZZZZ"):
            continue

        mtype = m.get("DataType", "")
        try:
            dimension = int(m.get("Dimension", "0"))
        except ValueError:
            dimension = 0

        hidden          = m.get("Hidden", "false").lower() == "true"
        external_access = m.get("ExternalAccess", "Read/Write")

        bit_number_raw = m.get("BitNumber")
        bit_number = None
        if bit_number_raw is not None:
            try:
                bit_number = int(bit_number_raw)
            except ValueError:
                pass

        desc_el     = m.find(".//Description")
        description = ""
        if desc_el is not None and desc_el.text:
            description = desc_el.text.strip().replace("<![CDATA[", "").replace("]]>", "")

        entry = {
            "name":            mname,
            "type":            mtype,
            "dimension":       dimension,
            "description":     description,
            "external_access": external_access,
            "hidden":          hidden,
        }
        if bit_number is not None:
            entry["bit_number"] = bit_number

        members.append(entry)
    return members


def _extract_verbatim_block(source: str, tag: str) -> str | None:
    """
    Extract an XML block verbatim from the source string, preserving CDATA
    sections exactly as authored.  ET.tostring() strips CDATA wrappers, which
    breaks Studio 5000's parser on DefaultData / Description fields inside AOI
    definitions.  We use simple depth-tracking instead.
    """
    import re as _re
    open_pat  = _re.compile(rf'<{_re.escape(tag)}(?:\s[^>]*)?>',  _re.DOTALL)
    close_tag = f'</{tag}>'

    m = open_pat.search(source)
    if not m:
        return None

    start = m.start()
    pos   = m.end()
    depth = 1

    while depth > 0 and pos < len(source):
        next_open  = source.find(f'<{tag}',  pos)
        next_close = source.find(close_tag, pos)

        if next_close == -1:
            return None   # malformed XML

        if next_open != -1 and next_open < next_close:
            depth += 1
            pos    = next_open + len(f'<{tag}')
        else:
            depth -= 1
            pos    = next_close + len(close_tag)

    return source[start:pos]


def _parse_dependencies_xml(dt_element, source: str = None, udt_name: str = None) -> str | None:
    """
    Extract the <Dependencies> block for a specific DataType verbatim from
    source (to preserve CDATA).  Falls back to ET serialisation if source is
    not provided.
    """
    if source and udt_name:
        # Find the DataType block for this UDT in the source, then extract
        # its Dependencies child verbatim.
        # Strategy: find <DataType ... Name="udt_name" ...> then look for
        # <Dependencies> before the next </DataType>.
        import re as _re
        dt_pat = _re.compile(
            rf'<DataType\b[^>]*\bName="{_re.escape(udt_name)}"[^>]*>',
            _re.DOTALL
        )
        m = dt_pat.search(source)
        if m:
            # Slice from the DataType opening tag to the next </DataType>
            close_dt = source.find('</DataType>', m.end())
            if close_dt != -1:
                dt_slice = source[m.start():close_dt + len('</DataType>')]
                return _extract_verbatim_block(dt_slice, 'Dependencies')

    # Fallback: ET serialisation (CDATA lost, but structurally correct)
    deps = dt_element.find("./Dependencies")
    if deps is None:
        return None
    return ET.tostring(deps, encoding="unicode")


def _extract_datatype_verbatim(source: str, udt_name: str) -> str | None:
    """
    Slice a complete <DataType ... Name="udt_name" ...>...</DataType> block
    verbatim from source, preserving CDATA and original formatting.

    Used to carry nested UDT context definitions through to single-UDT exports
    unchanged, so the output resolves on import. DataType elements never nest,
    so a simple search for the next </DataType> is safe.
    """
    if not source or not udt_name:
        return None
    import re as _re
    m = _re.search(
        rf'<DataType\b[^>]*\bName="{_re.escape(udt_name)}"[^>]*>',
        source, _re.DOTALL
    )
    if not m:
        return None
    close = source.find('</DataType>', m.end())
    if close == -1:
        return None
    return source[m.start():close + len('</DataType>')]


def _parse_aoi_context_xml(root, source: str = None) -> str | None:
    """
    Extract the <AddOnInstructionDefinitions Use="Context"> block verbatim
    from source to preserve CDATA in Parameter/LocalTag DefaultData fields
    and Description elements.

    Studio 5000 requires CDATA wrappers on DefaultData and many Description
    fields inside AOI definitions — ET.tostring() strips them.
    """
    if source:
        block = _extract_verbatim_block(source, 'AddOnInstructionDefinitions')
        if block:
            return block

    # Fallback: ET serialisation (loses CDATA)
    for el in root.findall(".//AddOnInstructionDefinitions"):
        return ET.tostring(el, encoding="unicode")
    return None


def _parse_aoi_registry(root) -> dict:
    """
    Extract all AddOnInstructionDefinition elements and estimate in-memory size.
    Returns: {aoi_name: size_bytes}
    """
    aoi_registry = {}
    for aoi in root.findall(".//AddOnInstructionDefinition"):
        name = aoi.get("Name")
        if not name:
            continue
        sz = _estimate_aoi_size(aoi)
        aoi_registry[name] = sz
        logging.info(f"AOI '{name}' registered — estimated size: {sz}B")
    return aoi_registry


# ---------------------------------------------------------------------------
# Main parsing entry point
# ---------------------------------------------------------------------------

def extract_all_udt_definitions(l5x_content: str) -> dict:
    """
    Parse an L5X file and return all user-defined DataTypes plus AOI metadata.

    Works for single-UDT exports (TargetType="DataType") and full controller
    exports (TargetType="Controller").

    Returns:
        {
          "l5x_type":       "single_udt" | "full_program",
          "target":         str | None,
          "udts":           {
            name: {
              "name":            str,
              "members":         [...],
              "is_target":       bool,
              "dependencies_xml": str | None,   # raw <Dependencies> XML
            }
          },
          "aoi_registry":   {name: size_bytes},
          "aoi_context_xml": str | None,   # raw <AddOnInstructionDefinitions Use="Context"> XML
          "error":          str   # only on failure
        }
    """
    try:
        root = ET.fromstring(l5x_content)
    except ET.ParseError as e:
        return {"error": f"Invalid L5X XML: {e}"}

    l5x_type    = detect_l5x_type(l5x_content)
    target_name = None
    all_udts    = {}

    # Capture source metadata to carry through to generated output
    sw_revision     = root.get("SoftwareRevision", "32.04")
    controller_name = None
    ctrl_el = root.find(".//Controller[@Use='Context']")
    if ctrl_el is not None:
        controller_name = ctrl_el.get("Name", "Controller")

    # Parse AOI definitions — sizes for estimation, raw XML verbatim for passthrough
    aoi_registry    = _parse_aoi_registry(root)
    aoi_context_xml = _parse_aoi_context_xml(root, source=l5x_content)

    if aoi_context_xml:
        logging.info("AOI context block found and will be carried through to output.")

    for dt in root.findall(".//DataType"):
        name = dt.get("Name")
        if not name:
            continue

        dt_class = dt.get("Class", "User")
        if dt_class == "System":
            continue

        is_target = dt.get("Use") == "Target"
        if is_target:
            target_name = name

        # Studio 5000 marks user-defined string types (STRING_40, STRING_64, …)
        # with Family="StringFamily". Track them so they sort with native STRING
        # instead of with arbitrary nested UDTs.
        is_string_family = dt.get("Family") == "StringFamily"

        members          = _parse_members(dt)
        dependencies_xml = _parse_dependencies_xml(dt, source=l5x_content, udt_name=name)
        verbatim_xml     = _extract_datatype_verbatim(l5x_content, name)

        all_udts[name] = {
            "name":             name,
            "members":          members,
            "is_target":        is_target,
            "is_string_family": is_string_family,
            "dependencies_xml": dependencies_xml,
            "verbatim_xml":     verbatim_xml,
            "controller_name":  controller_name or "Controller",
            "sw_revision":      sw_revision,
        }

    # Fallback target detection
    if not target_name:
        user_udts = list(all_udts.keys())
        if len(user_udts) == 1:
            target_name = user_udts[0]
            all_udts[target_name]["is_target"] = True
        elif l5x_type == "full_program":
            target_name = None
        else:
            return {"error": "No DataType with Use='Target' found in L5X."}

    # Cycle detection
    dep_graph = {
        n: {m["type"] for m in udt["members"] if m["type"] in all_udts}
        for n, udt in all_udts.items()
    }
    cycles = _detect_cycles(dep_graph)
    if cycles:
        return {"error": f"Circular UDT dependency detected: {'; '.join(cycles)}"}

    logging.info(
        f"L5X type: '{l5x_type}'. Extracted {len(all_udts)} user UDT(s), "
        f"{len(aoi_registry)} AOI(s). Target: '{target_name}'"
    )

    return {
        "l5x_type":        l5x_type,
        "target":          target_name,
        "udts":            all_udts,
        "aoi_registry":    aoi_registry,
        "aoi_context_xml": aoi_context_xml,
        "sw_revision":     sw_revision,
        "controller_name": controller_name or "Controller",
        "string_family_names": {n for n, u in all_udts.items() if u.get("is_string_family")},
    }


# ---------------------------------------------------------------------------
# Optimise a single UDT
# ---------------------------------------------------------------------------

def _members_order_changed(original_members: list, opt_member_names: list) -> bool:
    orig_names = [m["name"] for m in original_members
                  if not m.get("hidden") and not m["name"].startswith("ZZZZZZZZZZ")]
    return orig_names != opt_member_names


def _optimized_context_block(child_name: str, child_l5x: str, all_udts: dict) -> str | None:
    """
    Turn an optimized single-UDT L5X for `child_name` into a context-DataType block
    suitable for embedding as a sibling of a target.

    The generator emits the child as <DataType Use="Target" ...>; we splice the
    optimized body onto the child's ORIGINAL opening tag (verbatim) so the child's
    framing — its Use attribute (or absence of one), Family, Class — exactly matches
    what Studio 5000 produced. Only the member layout changes.
    """
    opt_block = _extract_datatype_block(child_l5x)   # <DataType Use="Target" ...>...</DataType>
    if not opt_block:
        return None

    # Inner = everything between the opening tag and the final </DataType>.
    first_gt   = opt_block.find(">")
    close_idx  = opt_block.rfind("</DataType>")
    if first_gt == -1 or close_idx == -1:
        return None
    inner = opt_block[first_gt + 1:close_idx]

    # Original opening <DataType ...> tag, preserved verbatim.
    verbatim = all_udts.get(child_name, {}).get("verbatim_xml")
    if verbatim:
        ot_end = verbatim.find(">")
        orig_open = verbatim[:ot_end + 1] if ot_end != -1 else None
    else:
        orig_open = None

    if not orig_open:
        # Fallback: demote Use="Target" to Use="Context" on the generated tag.
        orig_open = opt_block[:first_gt + 1].replace('Use="Target"', 'Use="Context"', 1)

    return f"{orig_open}{inner}</DataType>"


def _collect_nested_context_xml(target_name: str, all_udts: dict,
                                udt_size_registry: dict = None,
                                aoi_registry: dict = None) -> str | None:
    """
    Build the nested-UDT context for a single-UDT export: the OPTIMIZED
    <DataType> definitions of every user UDT the target transitively depends on
    (target excluded), emitted leaf-first.

    Each dependency is optimised in its own right (member reorder + BOOL packing)
    and embedded as a context sibling of the target, so the standalone export is
    both self-contained AND fully optimised. Leaf-first ordering mirrors Studio
    5000 and guarantees a dependency is laid out before anything that uses it.
    """
    if not target_name or target_name not in all_udts:
        return None
    if udt_size_registry is None: udt_size_registry = {}
    if aoi_registry      is None: aoi_registry      = {}

    # Transitive closure of UDT-typed dependencies.
    needed: set = set()
    stack = [target_name]
    while stack:
        cur = stack.pop()
        for m in all_udts.get(cur, {}).get("members", []):
            dep = m.get("type")
            if dep in all_udts and dep != target_name and dep not in needed:
                needed.add(dep)
                stack.append(dep)

    if not needed:
        return None

    # Emit leaf-first: filter the global topological order down to the closure.
    order  = [n for n in _topological_sort(all_udts) if n in needed]
    blocks = []
    for name in order:
        child_res = optimize_and_regenerate_udt(
            all_udts[name], all_udts=all_udts,
            udt_size_registry=udt_size_registry, aoi_registry=aoi_registry,
            aoi_context_xml=None, embed_nested_context=False,   # flat: siblings handled here
        )
        if child_res.get("success"):
            block = _optimized_context_block(name, child_res["udt_text"], all_udts)
        else:
            block = all_udts[name].get("verbatim_xml")   # fall back to verbatim if opt fails
            if block:
                block = block.strip()
            logging.warning(
                f"  Nested UDT '{name}' could not be optimised "
                f"({child_res.get('error')}); carrying it through verbatim."
            )
        if block:
            blocks.append(block)

    return "\n".join(blocks) if blocks else None


def optimize_and_regenerate_udt(udt_definition: dict, all_udts: dict = None,
                                 udt_size_registry: dict = None,
                                 aoi_registry: dict = None,
                                 aoi_context_xml: str = None,
                                 embed_nested_context: bool = False) -> dict:
    """
    Optimise and regenerate one UDT.

    AOI-typed members are carried through with their NullType radix intact.
    The output L5X will include:
      - <Dependencies> inside <DataType>  (from the original UDT's dependencies_xml)
      - <AddOnInstructionDefinitions Use="Context"> inside <Controller> (aoi_context_xml)

    Returns result dict with:
      optimization_needed: True | False | None
      size_before, size_after: int
      skipped_types, skipped_members: lists (truly unknown types only)
      aoi_members: list of member names carrying AOI types
    """
    if not isinstance(udt_definition, dict) or not udt_definition.get("name"):
        return {"success": False, "error": "Invalid UDT definition."}

    # Compatibility: extract_udt_definition() embeds the full parse context under
    # leading-underscore keys. If a caller forwards that enriched dict without
    # explicit kwargs, adopt the embedded context so nested UDTs are still
    # resolved and embedded rather than silently skipped.
    _adopted_ctx = False
    if all_udts is None and udt_definition.get("_all_udts"):
        all_udts = udt_definition["_all_udts"]
        _adopted_ctx = True
    if aoi_registry is None and udt_definition.get("_aoi_registry"):
        aoi_registry = udt_definition["_aoi_registry"]
    if aoi_context_xml is None and udt_definition.get("_aoi_context_xml"):
        aoi_context_xml = udt_definition["_aoi_context_xml"]

    # When context was adopted from a legacy single-arg call, embed nested
    # definitions so the standalone output is self-contained (unless the caller
    # explicitly opted out by passing embed_nested_context).
    if _adopted_ctx and not embed_nested_context:
        embed_nested_context = True

    if all_udts     is None: all_udts     = {}
    if aoi_registry is None: aoi_registry = {}

    if udt_size_registry is None:
        udt_size_registry = {}
        for name in _topological_sort(all_udts):
            udt_size_registry[name] = _estimate_udt_size(
                all_udts[name], udt_size_registry, aoi_registry
            )

    udt_name = udt_definition["name"]
    tags     = []

    skipped_type_set:    set  = set()   # unresolved types — reported, NOT dropped
    skipped_member_list: list = []      # members carrying an unresolved type
    aoi_member_list:     list = []
    optimizable_count = 0               # members with a resolvable (native/UDT/AOI) type

    for m in udt_definition.get("members", []):
        mname = m.get("name", "")
        if not mname:
            continue

        raw_type   = m.get("type", "DINT")
        base_upper = raw_type.upper()

        if base_upper == "BIT":
            base_type = "BOOL"
        else:
            base_type = base_upper

        is_native    = base_type in SUPPORTED_TYPES
        is_known_udt = raw_type in all_udts
        is_aoi       = raw_type in aoi_registry
        is_unknown   = (not is_native) and (not is_known_udt) and (not is_aoi)

        if is_unknown:
            # Unresolved type — record it for reporting, but DO NOT drop the
            # member. Dropping would silently delete the user's field. We carry
            # it through verbatim; the generator places it deterministically and
            # _estimate_udt_size already has a safe fallback for unknown types.
            skipped_type_set.add(raw_type)
            skipped_member_list.append(mname)
            logging.warning(
                f"  Carrying unresolved type '{raw_type}' on member '{mname}' "
                f"through unchanged — definition not found in this L5X "
                f"(may be in the full program file)."
            )
        else:
            optimizable_count += 1

        if is_aoi:
            aoi_member_list.append(mname)
            logging.info(
                f"  Carrying AOI member '{mname}' (type '{raw_type}') through unchanged."
            )

        if not is_native:
            # Preserve the original type name for UDTs, AOIs and unknowns.
            base_type = raw_type

        dimension = m.get("dimension", 0)
        type_str  = f"{base_type}[{dimension}]" if dimension > 0 else base_type

        tags.append({
            "name":            mname,
            "type":            type_str,
            "description":     m.get("description", mname),
            "external_access": m.get("external_access", "Read/Write"),
            "hidden":          m.get("hidden", False),
        })

    skipped_types_sorted   = sorted(skipped_type_set)
    skipped_members_sorted = sorted(skipped_member_list)
    aoi_members_sorted     = sorted(set(aoi_member_list))

    if skipped_types_sorted:
        logging.warning(
            f"  [{udt_name}] Unresolved types carried through (alphabetical): "
            + ", ".join(skipped_types_sorted)
        )
    if aoi_members_sorted:
        logging.info(
            f"  [{udt_name}] AOI members carried through: "
            + ", ".join(aoi_members_sorted)
        )

    if not tags:
        size_na = _estimate_udt_size(udt_definition, udt_size_registry, aoi_registry)
        return {
            "success":             False,
            "error":               (
                f"No members found for '{udt_name}'."
            ),
            "optimization_needed": None,
            "size_before":         size_na,
            "size_after":          size_na,
            "skipped_types":       skipped_types_sorted,
            "skipped_members":     skipped_members_sorted,
            "aoi_members":         aoi_members_sorted,
        }

    size_before = _estimate_udt_size(udt_definition, udt_size_registry, aoi_registry)

    # Pull the dependency XML from this specific UDT definition
    dependencies_xml = udt_definition.get("dependencies_xml")

    # For standalone single-UDT exports, carry nested UDT definitions through so
    # the output is self-contained and resolves on import.
    nested_context_xml = None
    if embed_nested_context and all_udts:
        nested_context_xml = _collect_nested_context_xml(
            udt_name, all_udts, udt_size_registry, aoi_registry
        )

    result = generate_udt_l5x_from_tags(
        {"udt_name": udt_name, "tags": tags},
        controller_name  = udt_definition.get("controller_name", "Controller"),
        sw_revision      = udt_definition.get("sw_revision", "32.04"),
        udt_registry     = udt_size_registry,
        aoi_registry     = aoi_registry,
        dependencies_xml = dependencies_xml,
        aoi_context_xml  = aoi_context_xml,
        nested_context_xml = nested_context_xml,
    )

    if result.get("success"):
        from L5XGen_UDT import member_sort_key as _sort_key
        import re as _re

        cleaned_for_sort = []
        for t in tags:
            raw = t["type"]
            m2  = _re.match(r"([A-Za-z_]\w*)(?:\[(\d*)\])?$", raw, _re.IGNORECASE)
            if not m2:
                continue
            base_raw, size_str = m2.groups()
            base_upper = base_raw.upper()
            dim = 0 if size_str is None else (1 if size_str == "" else int(size_str))
            is_native   = base_upper in SUPPORTED_TYPES
            is_nested   = (not is_native) and (base_raw in udt_size_registry)
            is_aoi_tag  = (not is_native) and (not is_nested) and (base_raw in aoi_registry)
            base_type   = base_upper if is_native else base_raw
            cleaned_for_sort.append({
                "name":          t["name"],
                "type":          base_type,
                "dimension":     dim,
                "is_nested_udt": is_nested or is_aoi_tag,
                "is_aoi":        is_aoi_tag,
                "hidden":        t.get("hidden", False),
            })

        sorted_tags = sorted(
            cleaned_for_sort,
            key=lambda x: _sort_key(
                x, udt_size_registry, aoi_registry,
                string_family_names={n for n, u in all_udts.items() if u.get("is_string_family")}
            )
        )

        opt_names  = [t["name"] for t in sorted_tags if not t.get("hidden")]
        opt_needed = _members_order_changed(udt_definition.get("members", []), opt_names)

        opt_members = []
        for t in sorted_tags:
            if t["type"].upper() == "BOOL" and t["dimension"] == 0:
                opt_members.append({"type": "BIT", "dimension": 0})
            else:
                opt_members.append({"type": t["type"], "dimension": t["dimension"]})
        size_after = _compute_member_list_size(opt_members, udt_size_registry, aoi_registry)

        result["optimization_needed"] = opt_needed
        result["size_before"]         = size_before
        result["size_after"]          = size_after
        result["skipped_types"]       = skipped_types_sorted
        result["skipped_members"]     = skipped_members_sorted
        result["aoi_members"]         = aoi_members_sorted
    else:
        result.setdefault("optimization_needed", False)
        result.setdefault("size_before",         size_before)
        result.setdefault("size_after",          size_before)
        result.setdefault("skipped_types",       skipped_types_sorted)
        result.setdefault("skipped_members",     skipped_members_sorted)
        result.setdefault("aoi_members",         aoi_members_sorted)

    return result


# ---------------------------------------------------------------------------
# Full-program batch optimise
# ---------------------------------------------------------------------------

def optimize_full_program_l5x(l5x_content: str) -> dict:
    """
    Parse a full controller L5X, optimise all user UDTs in dependency order,
    and return the modified L5X with only the <DataTypes> section replaced.
    Everything else (programs, tags, modules, AOIs, tasks) is untouched.
    """
    parsed = extract_all_udt_definitions(l5x_content)
    if "error" in parsed:
        return {"success": False, "error": parsed["error"]}

    all_udts        = parsed["udts"]
    aoi_registry    = parsed.get("aoi_registry", {})
    aoi_context_xml = parsed.get("aoi_context_xml")

    if not all_udts:
        return {"success": False, "error": "No user-defined DataTypes found in L5X."}

    udt_size_registry = {}
    for name in _topological_sort(all_udts):
        udt_size_registry[name] = _estimate_udt_size(
            all_udts[name], udt_size_registry, aoi_registry
        )

    optimized_xml_map = {}
    results           = []

    for name in _topological_sort(all_udts):
        udt = all_udts[name]
        res = optimize_and_regenerate_udt(
            udt,
            all_udts=all_udts,
            udt_size_registry=udt_size_registry,
            aoi_registry=aoi_registry,
            aoi_context_xml=None,   # not needed for full-program — AOIs stay in place
        )
        ok = res.get("success", False)
        results.append({
            "name":         name,
            "success":      ok,
            "optimization_needed": res.get("optimization_needed", False),
            "size_before":  res.get("size_before"),
            "size_after":   res.get("size_after"),
            "error":        res.get("error") if not ok else None,
            "member_count": len(udt.get("members", [])),
        })
        if ok:
            inner = _extract_datatype_block(res["udt_text"])
            if inner:
                optimized_xml_map[name] = inner

    try:
        modified_l5x = _replace_datatype_blocks(l5x_content, optimized_xml_map, all_udts)
    except Exception as e:
        return {"success": False, "error": f"Failed to rebuild L5X: {e}"}

    succeeded = sum(1 for r in results if r["success"])
    logging.info(f"Batch complete: {succeeded}/{len(results)} UDTs optimised.")

    return {
        "success":       True,
        "l5x_text":      modified_l5x,
        "download_name": "Optimized_Program.l5x",
        "results":       results,
    }


def _extract_datatype_block(udt_l5x: str) -> str | None:
    """
    Pull the <DataType Use="Target" ...>...</DataType> element from a single-UDT L5X.
    Preserves <Dependencies> child if present.
    """
    try:
        root = ET.fromstring(udt_l5x)
        dt   = root.find(".//DataType[@Use='Target']")
        if dt is None:
            return None
        return ET.tostring(dt, encoding="unicode")
    except ET.ParseError:
        return None


def _replace_datatype_blocks(original_l5x: str, optimized_map: dict,
                              original_udts: dict = None) -> str:
    """
    Parse the original full-program L5X and swap out each <DataType> element
    whose name is in optimized_map.

    Preserves verbatim:
    - Use/Class/Family attributes from the original element
    - <Dependencies> blocks from original_udts (verbatim, CDATA intact)

    Everything else (programs, tags, modules, AOIs, tasks) is untouched.
    """
    if original_udts is None:
        original_udts = {}

    root = ET.fromstring(original_l5x)

    for dt_parent in root.findall(".//DataTypes"):
        to_remove = []
        for dt in list(dt_parent):
            name = dt.get("Name")
            if name and name in optimized_map:
                idx = list(dt_parent).index(dt)
                to_remove.append((idx, dt, name))

        for idx, orig_dt, name in reversed(to_remove):
            new_dt = ET.fromstring(optimized_map[name])

            # Carry over Use/Class/Family attributes
            for attr in ("Use", "Class", "Family"):
                orig_val = orig_dt.get(attr)
                if orig_val and not new_dt.get(attr):
                    new_dt.set(attr, orig_val)

            # Remove any Dependencies child the generator may have added
            # (injected as raw string — won't round-trip through ET cleanly)
            new_deps = new_dt.find("./Dependencies")
            if new_deps is not None:
                new_dt.remove(new_deps)

            dt_parent.remove(orig_dt)
            dt_parent.insert(idx, new_dt)

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    result = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body

    # Re-inject verbatim Dependencies blocks after ET serialisation
    import re as _re
    for name, udt in (original_udts or {}).items():
        if name not in optimized_map:
            continue
        deps_xml = udt.get("dependencies_xml")
        if not deps_xml:
            continue
        dt_open = _re.search(
            rf'<DataType\b[^>]*\bName="{_re.escape(name)}"[^>]*>', result
        )
        if dt_open:
            members_close = result.find("</Members>", dt_open.end())
            if members_close != -1:
                insert_pos = members_close + len("</Members>")
                result = result[:insert_pos] + f"\n{deps_xml.strip()}" + result[insert_pos:]

    return result


# ---------------------------------------------------------------------------
# Backward-compatibility shims (LLM4UDT integration)
# ---------------------------------------------------------------------------
# LLM4UDT historically used a single-UDT-only optimizer whose public entry was
# extract_udt_definition(content) -> {"name", "members"} and a single-argument
# optimize_and_regenerate_udt(udt_def). The full port above replaces that core
# with the nested-aware engine. These shims preserve the old call surface so the
# NLP, CSV and attach pipelines keep working, while the parse helper now ALSO
# returns the full UDT map needed for nested-context embedding.

def extract_udt_definition(l5x_content: str) -> dict:
    """
    Compatibility wrapper around extract_all_udt_definitions().

    Returns the target UDT in the legacy shape:
        {"name": str, "members": [...], "_all_udts": {...},
         "_aoi_registry": {...}, "_aoi_context_xml": str|None}
    or {"error": str} on failure.

    The leading-underscore keys carry the full parse context so callers that
    want nested-UDT support can forward it into optimize_and_regenerate_udt;
    older callers that only read name/members are unaffected.
    """
    parsed = extract_all_udt_definitions(l5x_content)
    if "error" in parsed:
        return {"error": parsed["error"]}

    target = parsed.get("target")
    all_udts = parsed.get("udts", {})
    if not target or target not in all_udts:
        # Fall back to the sole UDT if there is exactly one.
        if len(all_udts) == 1:
            target = next(iter(all_udts))
        else:
            return {"error": "No target DataType found in L5X."}

    tgt = dict(all_udts[target])          # shallow copy of the target definition
    tgt["_all_udts"]         = all_udts
    tgt["_aoi_registry"]     = parsed.get("aoi_registry", {})
    tgt["_aoi_context_xml"]  = parsed.get("aoi_context_xml")
    return tgt
