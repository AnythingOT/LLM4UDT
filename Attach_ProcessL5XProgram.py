# Attach_ProcessL5XProgram.py
# Shared file — keep identical between LLM UDT Generator and L5X UDT Optimizer.
#
# Handles a full ControlLogix .L5X program export (TargetType="Controller").
# Extracts all user-defined DataTypes, builds a dependency graph, resolves
# processing order via topological sort (Kahn's algorithm), analyses each UDT,
# and optionally rewrites only the <Members> blocks in-place — leaving every
# other section (Programs, Tags, AOIs, IO, Tasks, etc.) completely untouched.

import xml.etree.ElementTree as ET
import logging
from collections import defaultdict, deque

# Delegate classification & sort to the engine. The previous local
# DATA_TYPE_ORDER table did not match member_sort_key on arrays, LINT/LREAL/
# COUNTER/CONTROL/MESSAGE, nested UDTs, AOIs, or natural-number sort — meaning
# the analysis preview disagreed with what the optimizer actually produces.
from L5XGen_UDT import member_sort_key, SUPPORTED_TYPES

logger = logging.getLogger(__name__)


def _is_visible(m: ET.Element) -> bool:
    return m.attrib.get('Hidden', 'false').lower() != 'true'


def _member_dict_for_sort(m: ET.Element) -> dict:
    """
    Map an ET <Member> element into the dict shape member_sort_key expects.
    BIT (packed scalar BOOL) is normalised to BOOL so it sorts with scalar BOOLs.
    """
    dtype = m.attrib.get('DataType', '')
    if dtype.upper() == 'BIT':
        dtype = 'BOOL'
    try:
        dim = int(m.attrib.get('Dimension', '0'))
    except ValueError:
        dim = 0
    return {'name': m.attrib.get('Name', ''), 'type': dtype, 'dimension': dim}


def _category_label(m: ET.Element, udt_names: set, aoi_names: set) -> str:
    """
    Human-readable category derived from the same group decision member_sort_key
    uses — so the UI label always agrees with the sort position.
    """
    md = _member_dict_for_sort(m)
    base = md['type'].upper()
    dim  = md['dimension']
    is_native = base in SUPPORTED_TYPES
    is_udt    = (not is_native) and (md['type'] in udt_names)
    is_aoi    = (not is_native) and (not is_udt) and (md['type'] in aoi_names)

    if dim > 0:
        if is_udt:    return f"UDT {md['type']} array"
        if is_aoi:    return f"AOI {md['type']} array"
        if is_native: return f"{base} array"
        return f"{md['type']} array"
    if is_aoi:    return f"AOI ({md['type']})"
    if is_udt:    return f"UDT ({md['type']})"
    if base == 'BOOL':
        return 'BOOL (scalar)'
    if is_native: return base
    return md['type']


def _optimal_order(members: list, udt_names: set, aoi_names: set) -> list[str]:
    """Visible member names in the optimizer's true output order."""
    udt_reg = {n: 0 for n in udt_names}   # registry presence is all sort needs
    aoi_reg = {n: 0 for n in aoi_names}
    visible = [m for m in members if _is_visible(m)]
    ordered = sorted(
        visible,
        key=lambda m: member_sort_key(_member_dict_for_sort(m), udt_reg, aoi_reg)
    )
    return [m.attrib.get('Name', '') for m in ordered]


# ── Dependency resolution ─────────────────────────────────────────────────────

def _build_dependency_graph(udt_elements: list[ET.Element]) -> tuple[dict, dict]:
    """
    Returns:
      dep_map  — {udt_name: [list of user-UDT names it depends on]}
      elem_map — {udt_name: ET.Element}
    """
    name_set = {el.get('Name') for el in udt_elements}
    dep_map  = {}
    elem_map = {}

    for el in udt_elements:
        name = el.get('Name')
        elem_map[name] = el
        deps = []
        for m in el.findall('./Members/Member'):
            dtype = m.attrib.get('DataType', '')
            if dtype in name_set and dtype != name:
                if dtype not in deps:
                    deps.append(dtype)
        dep_map[name] = deps

    return dep_map, elem_map


def _topological_sort(dep_map: dict) -> tuple[list[str], list[str]]:
    """
    Kahn's algorithm.
    Returns (ordered_names, cyclic_names).
    cyclic_names will be non-empty only if there are circular references
    (invalid in ControlLogix, but we handle it gracefully).
    """
    names     = list(dep_map.keys())
    in_degree = {n: 0 for n in names}
    graph     = defaultdict(list)   # dep → [dependents that need it first]

    for name, deps in dep_map.items():
        for dep in deps:
            if dep in in_degree:
                graph[dep].append(name)
                in_degree[name] += 1

    queue  = deque(n for n in names if in_degree[n] == 0)
    order  = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for dependent in graph[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    cyclic = [n for n in names if n not in order]
    return order, cyclic


# ── Single-UDT analysis (mirrors Attach_UDTVerify.analyse_udt) ───────────────

def _analyse_element(el: ET.Element, udt_names: set, aoi_names: set) -> dict:
    """Analyse one DataType element — returns the same report dict as analyse_udt()."""
    name    = el.get('Name', 'Unknown')
    members = list(el.findall('./Members/Member'))
    visible = [m for m in members if _is_visible(m)]

    issues  = []

    # Sort order
    current  = [m.attrib.get('Name', '') for m in visible]
    optimal  = _optimal_order(members, udt_names, aoi_names)
    if current != optimal:
        for i, (c, o) in enumerate(zip(current, optimal)):
            if c != o:
                issues.append({'type': 'sort_order', 'severity': 'warning',
                               'detail': f"Member '{c}' at pos {i+1}, expected '{o}'."})
                break

    # Unpacked scalar BOOLs
    unpacked = [m for m in visible if m.attrib.get('DataType') == 'BOOL'
                and int(m.attrib.get('Dimension', '0')) == 0]
    if unpacked:
        bad_names = ', '.join(m.attrib.get('Name', '?') for m in unpacked)
        issues.append({'type': 'bool_packing', 'severity': 'warning',
                       'detail': f"Scalar BOOL not bit-packed: {bad_names}."})

    # Invalid BOOL array sizes
    valid_sizes = set(range(32, 1025, 32))
    for m in visible:
        if m.attrib.get('DataType') == 'BOOL':
            dim = int(m.attrib.get('Dimension', '0'))
            if dim > 0 and dim not in valid_sizes:
                issues.append({'type': 'bool_packing', 'severity': 'error',
                               'detail': f"'{m.attrib.get('Name')}' is BOOL[{dim}] — must be multiple of 32."})

    # Per-member detail
    name_to_exp = {n: i for i, n in enumerate(optimal)}
    member_list = []
    for i, m in enumerate(visible):
        mname = m.attrib.get('Name', '')
        dtype = m.attrib.get('DataType', '')
        exp   = name_to_exp.get(mname, i)
        member_list.append({
            'name': mname, 'type': dtype,
            'dimension': int(m.attrib.get('Dimension', '0')),
            'category': _category_label(m, udt_names, aoi_names),
            'position': i, 'expected_position': exp,
            'out_of_order': i != exp,
        })

    return {
        'udt_name':           name,
        'member_count':       len(visible),
        'issues':             issues,
        'members':            member_list,
        'needs_optimization': bool(issues),
        'summary':            f"{len(issues)} issue(s) found." if issues else "Already optimized.",
    }


# ── (Removed) _reorder_members_inplace ────────────────────────────────────────
# Dead code: optimize_program() below delegates the rewrite entirely to
# L5XOpt_UDT.optimize_full_program_l5x, which uses the engine's sort/pack rules.
# Keeping a second reorder path here only invited drift from the engine.


# ── Public API ────────────────────────────────────────────────────────────────

def analyse_program(l5x_content: str) -> dict:
    """
    Analyse all user UDTs in a full program L5X.

    Returns:
    {
        "controller_name": str,
        "udt_count":       int,
        "needs_opt_count": int,
        "processing_order": [str],          # topo-sorted names
        "cyclic":          [str],            # names with circular deps (should be empty)
        "analyses":        {name: analysis_dict},
        "error":           str | None
    }
    """
    try:
        root = ET.fromstring(l5x_content)
    except ET.ParseError as e:
        return {"error": f"Invalid XML: {e}"}

    controller_name = root.get('TargetName', 'Unknown')
    # ET doesn't support attribute axis — get it differently
    ctrl = root.find('.//Controller')
    if ctrl is not None:
        controller_name = ctrl.get('Name', controller_name)

    user_udts = [el for el in root.findall('.//DataType')
                 if el.get('Class') == 'User']

    if not user_udts:
        return {"error": "No user-defined DataTypes found in this L5X file."}

    dep_map, elem_map = _build_dependency_graph(user_udts)
    order, cyclic     = _topological_sort(dep_map)

    # Sibling-UDT names (for nested-UDT classification) and AOI names.
    udt_names = set(elem_map.keys())
    aoi_names = {a.get('Name') for a in root.findall('.//AddOnInstructionDefinition')
                 if a.get('Name')}

    if cyclic:
        logger.warning(f"Circular UDT references detected: {cyclic}")

    analyses = {}
    for name in order:
        el = elem_map.get(name)
        if el is not None:
            analyses[name] = _analyse_element(el, udt_names, aoi_names)

    needs_opt_count = sum(1 for a in analyses.values() if a['needs_optimization'])

    return {
        "controller_name":  controller_name,
        "udt_count":        len(user_udts),
        "needs_opt_count":  needs_opt_count,
        "processing_order": order,
        "cyclic":           cyclic,
        "analyses":         analyses,
        "error":            None,
    }


def optimize_program(l5x_content: str) -> dict:
    """
    Optimize all user UDTs in a full controller L5X.

    Delegates to the ported nested-aware engine (L5XOpt_UDT.optimize_full_program_l5x),
    which optimizes every user DataType in dependency order — including nested UDTs,
    which are now reordered/packed in their own right instead of being shoved into an
    'OTHER' bucket. Everything outside <DataTypes> (programs, tags, modules, AOIs,
    tasks) is preserved.

    Returns the legacy shape expected by app.py:
    {
        "success":      bool,
        "optimized_xml": str,
        "changed":      [str],   # UDT names that were modified
        "skipped":      [str],   # already optimal / unchanged
        "cyclic":       [str],   # could not be processed (circular refs)
        "error":        str | None
    }
    """
    from L5XOpt_UDT import optimize_full_program_l5x, extract_all_udt_definitions

    # Pre-parse to surface cyclic dependencies in the legacy 'cyclic' field.
    # (The engine treats a cycle as a hard error; here we report it gracefully.)
    parsed = extract_all_udt_definitions(l5x_content)
    if isinstance(parsed, dict) and parsed.get("error"):
        if "Circular" in parsed["error"] or "cycle" in parsed["error"].lower():
            # Fall back to per-UDT analysis to list the cyclic names for the UI.
            try:
                root        = ET.fromstring(l5x_content)
                user_udts   = [el for el in root.findall('.//DataType') if el.get('Class') == 'User']
                dep_map, _  = _build_dependency_graph(user_udts)
                _, cyclic   = _topological_sort(dep_map)
            except Exception:
                cyclic = []
            return {"success": False, "optimized_xml": l5x_content,
                    "changed": [], "skipped": [], "cyclic": cyclic,
                    "error": parsed["error"]}
        return {"success": False, "optimized_xml": l5x_content,
                "changed": [], "skipped": [], "cyclic": [], "error": parsed["error"]}

    result = optimize_full_program_l5x(l5x_content)
    if not result.get("success"):
        return {"success": False, "optimized_xml": l5x_content,
                "changed": [], "skipped": [], "cyclic": [],
                "error": result.get("error", "Optimization failed.")}

    # Map engine per-UDT results to changed/skipped using optimization_needed.
    changed, skipped = [], []
    for r in result.get("results", []):
        name = r.get("name")
        if not name:
            continue
        if r.get("success") and r.get("optimization_needed"):
            changed.append(name)
        else:
            skipped.append(name)

    return {
        "success":       True,
        "optimized_xml": result["l5x_text"],
        "changed":       changed,
        "skipped":       skipped,
        "cyclic":        [],
        "error":         None,
    }
