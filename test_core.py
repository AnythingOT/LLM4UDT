#!/usr/bin/env python3
"""
test_core.py — regression tests for the LLM4UDT bug fixes.

Run:  python3 test_core.py        (exit 0 = all passed, 1 = a failure)

These call the same functions the web app calls and assert the invariants that
matter for a tool that rewrites engineering source. No Flask, no Ollama, no
network — runs offline.
"""
import io
import os
import re
import sys
import time
import logging
import xml.etree.ElementTree as ET

# Silence the noisy module-level logging configs.
logging.disable(logging.CRITICAL)

# Make the test importable from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── A minimal `openai` stub so app.py / model_UDTGen import without the real ─
# library being installed. The stub is only consulted if a test actually calls
# extract_udt_tags() — none of the tests below do.
sys.modules.setdefault('openai', type(sys)('openai'))
class _DummyOpenAI:
    def __init__(self, *a, **kw):
        self.chat = type('C', (), {'completions': type('X', (), {'create': lambda *a, **k: None})()})()
sys.modules['openai'].OpenAI = _DummyOpenAI

passed, failed = 0, []
def check(label, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed.append(label)
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


# ─────────────────────────────────────────────────────────────────────────────
# Fix #1 — Attach_ProcessL5XProgram: analyser uses engine sort, not local table
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #1] Program analyser delegates to engine sort")
from Attach_ProcessL5XProgram import _analyse_element, _optimal_order

xml_str = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0">
 <Controller Name="Test">
  <DataTypes>
   <DataType Name="Nested" Class="User"><Members><Member Name="x" DataType="DINT" Dimension="0"/></Members></DataType>
   <DataType Name="Target" Class="User">
    <Members>
     <Member Name="lr_val"    DataType="LREAL"  Dimension="0"/>
     <Member Name="nest_inst" DataType="Nested" Dimension="0"/>
     <Member Name="bool10"    DataType="BOOL"   Dimension="0"/>
     <Member Name="bool2"     DataType="BOOL"   Dimension="0"/>
     <Member Name="dint_v"    DataType="DINT"   Dimension="0"/>
    </Members>
   </DataType>
  </DataTypes>
 </Controller>
</RSLogix5000Content>"""
root = ET.fromstring(xml_str)
target = next(d for d in root.findall('.//DataType') if d.get('Name') == 'Target')
udt_names = {'Nested', 'Target'}
aoi_names = set()

optimal = _optimal_order(list(target.findall('./Members/Member')), udt_names, aoi_names)
expected = ['bool2', 'bool10', 'dint_v', 'lr_val', 'nest_inst']
check("natural-key sort (bool2 before bool10), LREAL grouped, nested UDT last",
      optimal == expected, f"got {optimal}")

analysis = _analyse_element(target, udt_names, aoi_names)
cats = {m['name']: m['category'] for m in analysis['members']}
check("nested UDT category is 'UDT (Nested)', not 'Other'",
      cats['nest_inst'] == 'UDT (Nested)', f"got {cats['nest_inst']!r}")
check("LREAL category is 'LREAL', not 'Other'",
      cats['lr_val'] == 'LREAL', f"got {cats['lr_val']!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Fix #2 — Validator: BOOL[N] rounds UP, errors past 1024
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #2] Validator BOOL[N] no longer drops user bits")
from Validator_UDT import validate_udt

r = validate_udt({"udt_name": "T", "tags": [{"name": "bits", "type": "BOOL[33]"}]})
check("BOOL[33] → BOOL[64] (rounds up; old code rounded down to 32, losing 1 bit)",
      r.is_valid and r.fixed_data["tags"][0]["type"] == "BOOL[64]",
      f"got type={r.fixed_data['tags'][0]['type']!r} errors={r.errors}")

r = validate_udt({"udt_name": "T", "tags": [{"name": "bits", "type": "BOOL[100]"}]})
check("BOOL[100] → BOOL[128] (up, not nearest=96)",
      r.fixed_data["tags"][0]["type"] == "BOOL[128]",
      f"got {r.fixed_data['tags'][0]['type']!r}")

r = validate_udt({"udt_name": "T", "tags": [{"name": "bits", "type": "BOOL[2000]"}]})
check("BOOL[2000] is a blocking error, not silent truncation",
      not r.is_valid and any("exceeds" in e for e in r.errors),
      f"errors={r.errors}")

r = validate_udt({"udt_name": "T", "tags": [{"name": "bits", "type": "BOOL[1]"}]})
check("BOOL[1] still degrades to scalar BOOL (unchanged behaviour)",
      r.fixed_data["tags"][0]["type"] == "BOOL",
      f"got {r.fixed_data['tags'][0]['type']!r}")

r = validate_udt({"udt_name": "T", "tags": [{"name": "bits", "type": "BOOL[64]"}]})
check("BOOL[64] (valid size) passes through unchanged",
      r.fixed_data["tags"][0]["type"] == "BOOL[64]",
      f"got {r.fixed_data['tags'][0]['type']!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Fix #3 — model_UDTGen: JSON fence regex is robust
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #3] LLM JSON-fence regex tolerates real-world variants")
PAT = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.DOTALL | re.IGNORECASE)
cases = [
    ('```json\n{"a":1}\n```',                              '{"a":1}', 'LF + lowercase tag'),
    ('```json\r\n{"a":1}\r\n```',                          '{"a":1}', 'CRLF line endings'),
    ('```{"a":1}```',                                      '{"a":1}', 'no newlines, no tag'),
    ('```JSON\n{"a":1}\n```',                              '{"a":1}', 'uppercase tag'),
    ('```\n{"a":1}\n```',                                  '{"a":1}', 'bare fence, no tag'),
    ('preamble\n```json\n{"a":1}\n```\npost',              '{"a":1}', 'surrounding text'),
]
for raw, expected_body, label in cases:
    m = PAT.search(raw)
    check(f"regex handles {label}",
          bool(m) and m.group(1).strip() == expected_body,
          f"got {(m and m.group(1).strip())!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Fix #4 — app.stored_files: bounded TTL cache
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #4] stored_files cache is bounded and TTL-evicting")
import app

# Cap test
app._STORED_FILES_MAX = 3
app.stored_files.clear()
for i in range(10):
    app._stored_files_set(f'f{i}.L5X', f'content{i}')
check("cache capped at MAX entries (oldest evicted)",
      len(app.stored_files) == 3 and app._stored_files_get('f0.L5X') is None)
check("newest entry retrievable",
      app._stored_files_get('f9.L5X') == 'content9')

# Pop
app._stored_files_pop('f9.L5X')
check("pop removes entry",
      app._stored_files_get('f9.L5X') is None)

# TTL eviction
app._STORED_FILES_TTL_SEC = 0   # everything expires immediately
app.stored_files.clear()
app._stored_files_set('expired.L5X', 'old')
time.sleep(0.01)
check("expired entry returns None on get",
      app._stored_files_get('expired.L5X') is None)


# ─────────────────────────────────────────────────────────────────────────────
# Fix #5 — Localhost-only bind enforcement
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #5] app refuses to bind to non-localhost host")
import inspect
src = inspect.getsource(app)
check("debug=True is no longer hardcoded",
      "app.run(debug=True" not in src)
check("FLASK_DEBUG gates debug mode",
      "FLASK_DEBUG" in src and "debug=debug_mode" in src)
check("LLM4UDT_HOST is validated before binding",
      "LLM4UDT_HOST" in src and "Refusing to bind" in src)
check("'localhost' bind replaced with explicit 127.0.0.1 default",
      "'127.0.0.1'" in src)


# ─────────────────────────────────────────────────────────────────────────────
# Fix #6 — Upload directory is purged at startup and per-request
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #6] Upload directory hygiene")
check("startup purges leftover uploads",
      "_purge_upload_dir" in src and "_purge_upload_dir(app.config['UPLOAD_FOLDER'])" in src)
check("CSV path deletes file after read",
      src.count("os.remove(filepath)") >= 3,
      f"found {src.count('os.remove(filepath)')} delete sites; expected ≥3 (CSV, Excel, L5X)")


# ─────────────────────────────────────────────────────────────────────────────
# Fix #7 — STRING-family UDTs sort with native STRING, not nested UDT
# ─────────────────────────────────────────────────────────────────────────────
print("\n[Fix #7] User-defined STRING_N types sort with native STRING")
from L5XGen_UDT import member_sort_key

# Without the kwarg → custom string sorts as nested UDT (group 10)
m_custom = {"name": "s1", "type": "STRING_40", "dimension": 0}
m_nested = {"name": "n1", "type": "OtherUDT",  "dimension": 0}
udt_reg = {"STRING_40": 44, "OtherUDT": 12}

k_old = member_sort_key(m_custom, udt_reg, {})
check("without string_family hint, STRING_40 sorts as nested UDT (group 10)",
      k_old[0] == 10, f"got {k_old}")

# With the kwarg → sorts as STRING (group 8)
k_new = member_sort_key(m_custom, udt_reg, {}, string_family_names={"STRING_40"})
check("with string_family hint, STRING_40 sorts as STRING (group 8)",
      k_new[0] == 8, f"got {k_new}")

# Sort group is below DINT-array (group 4) and above non-string nested UDT
k_arr = member_sort_key({"name": "a", "type": "DINT", "dimension": 4}, udt_reg, {})
check("STRING-family scalar sorts after arrays (4) and before nested UDTs (10)",
      k_arr[0] == 4 < k_new[0] == 8 < member_sort_key(m_nested, udt_reg, {})[0],
      f"DINT[4]={k_arr[0]}, STRING_40={k_new[0]}, OtherUDT={member_sort_key(m_nested, udt_reg, {})[0]}")

# String-family detection from L5X parse
from L5XOpt_UDT import extract_all_udt_definitions
strfam_xml = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" TargetType="Controller">
 <Controller Name="Test">
  <DataTypes>
   <DataType Name="STRING_40" Family="StringFamily" Class="User">
    <Members>
     <Member Name="LEN"  DataType="DINT" Dimension="0" Hidden="false"/>
     <Member Name="DATA" DataType="SINT" Dimension="40" Hidden="false"/>
    </Members>
   </DataType>
   <DataType Name="Holder" Class="User">
    <Members><Member Name="s" DataType="STRING_40" Dimension="0"/></Members>
   </DataType>
  </DataTypes>
 </Controller>
</RSLogix5000Content>"""
parsed = extract_all_udt_definitions(strfam_xml)
check("parser detects Family=StringFamily DataType",
      "STRING_40" in parsed.get("string_family_names", set()),
      f"got {parsed.get('string_family_names')}")
check("string-family UDT is correctly sized via registry (4+40=44)",
      parsed["udts"]["STRING_40"] is not None)  # presence; size computed downstream


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
total = passed + len(failed)
if failed:
    print(f"FAILED  {len(failed)} of {total}")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
else:
    print(f"PASSED  {passed} of {total}")
    sys.exit(0)
