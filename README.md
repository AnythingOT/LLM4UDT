# LLM4UDT

A local web app that generates and optimizes Rockwell ControlLogix **User-Defined Data Types** (UDTs) for export to Studio 5000 as `.L5X` files.

Three input modes:

1. **Natural language** — describe a UDT in plain English, a local Ollama model extracts the field list, the app validates and emits an L5X.
2. **L5X attachment** — drop in an existing UDT or full-controller export, get back an optimized version with members reordered for minimum memory footprint and BOOLs bit-packed.
3. **CSV / Excel batch** — one tab or row-group per UDT, output is a zip of L5X files.


---

## Why this exists

Rockwell ControlLogix lays UDT members out in declaration order with natural alignment. Authoring a UDT carelessly — a BOOL between two DINTs, scalar BOOLs scattered through the struct, REALs interleaved with INTs — adds padding bytes that compound across every instance. For a UDT instantiated thousands of times (motor objects, valve objects, recipe slots), wasted bytes per instance × instance count is real controller memory.

This tool reorders members into a deterministic, padding-aware layout and packs scattered scalar BOOLs into SINT bit-fields, matching what Studio 5000 does internally. Nested UDTs and AOIs are handled; CDATA in descriptions and AOI defaults is preserved verbatim (the usual ElementTree gotcha that breaks Studio 5000 imports).

---

## Install

Requires Python 3.10+ and (for mode #1) [Ollama](https://ollama.com/) running locally with at least one model pulled.

```bash
pip install -r requirements.txt
ollama pull phi4          # or any model you prefer
```

## Run

```bash
python3 app.py
```

Opens on `http://127.0.0.1:5003`. The app **hard-binds to localhost** and refuses to start on a non-loopback interface — there's no auth and it reads/writes engineering source files. If you really need a different host, set `LLM4UDT_HOST=127.0.0.1` (or run it behind a reverse proxy with auth in front).

Optional environment variables:

| Variable          | Default       | Purpose                                         |
|-------------------|---------------|-------------------------------------------------|
| `LLM4UDT_HOST`    | `127.0.0.1`   | Bind address. Only loopback values accepted.    |
| `FLASK_DEBUG`     | `0`           | Set to `1` to enable Werkzeug debugger (RCE).   |

---

## Usage

### Mode 1 — Natural language

Open Settings (⚙️), pick an Ollama model, then in the chat box:

> Create a UDT named MotorStatus with: run_status, BOOL, indicates if motor is running; fault_code, DINT; speed_setpoint, REAL; alarm_bits, BOOL[64]; phase_currents, REAL[3]

Field syntax is strict — fields separated by **`;`**, values within a field by **`,`** (name, type, optional description). Supported types: `BOOL`, `SINT`, `INT`, `DINT`, `REAL`, `STRING`, `TIMER`, `COUNTER` plus arrays `TYPE[N]`. BOOL arrays must be multiples of 32 up to 1024.

### Mode 2 — L5X attachment

Drag in a `.L5X` file (single UDT or full controller export). The app reports issues found (sort order, unpacked BOOLs, invalid BOOL array sizes) and offers an Optimize button. Output is a download.

For a full-program L5X, only the `<DataTypes>` section is rewritten — programs, tags, modules, AOIs, and tasks pass through untouched.

### Mode 3 — CSV / Excel batch

CSV columns (case-insensitive): `UDT_Name`, `Tag_Name`, `Data_Type`, `Description`. Rows grouped by `UDT_Name`. Excel: one sheet per UDT, sheet name = UDT name. Output: a zip of L5X files.

---

## Optimization rules

Members are sorted into groups (then natural-key by name within each group):

| Group | Contents                                            |
|------:|-----------------------------------------------------|
| 0     | BOOL scalars (packed into SINT bit-fields)          |
| 1–3   | SINT, INT, DINT scalars                             |
| 4     | All arrays (sub-sorted BOOL → SINT → INT → DINT → REAL → LREAL → LINT → STRING → other) |
| 5–7   | REAL, LREAL, LINT scalars                           |
| 8     | STRING scalars (including `Family="StringFamily"` user types like `STRING_40`) |
| 9     | TIMER, COUNTER, CONTROL, MESSAGE, AOI instances     |
| 10    | Nested UDT scalars                                  |
| 11    | Unresolved / unknown types                          |

Byte sizes follow Rockwell layout: natural alignment per type, BOOL[N] stored as `ceil(N/32) × 4` bytes, consecutive BIT members packed into a single SINT block, final struct rounded to maximum member alignment.

Unresolved types are **never silently dropped** — they're carried through verbatim with a warning, so a UDT referencing a type defined elsewhere in the program won't lose members.

---

## Tests

```bash
python3 test_core.py
```

29 regression tests, no external dependencies (Ollama is stubbed). Exits 0 on success.

Test areas:

- **Engine-consistent sort** in the program analyser (no stale local sort tables).
- **BOOL[N] rounds up, never down** — `BOOL[33]` becomes `BOOL[64]`, not `BOOL[32]`. `BOOL[>1024]` is a blocking error, not silent truncation.
- **LLM JSON fence parsing** tolerant of CRLF, missing language tag, uppercase tags, missing trailing newlines.
- **Stored-file cache** is bounded (capped + TTL-evicted) and thread-safe.
- **Localhost-only enforcement** at startup.
- **Upload directory** purged at startup and per-request.
- **String-family UDTs** (`STRING_40` etc.) sort with native STRING, not as arbitrary nested UDTs.

---

## File layout

| File                          | Purpose                                                       |
|-------------------------------|---------------------------------------------------------------|
| `app.py`                      | Flask routes and request handling                             |
| `model_UDTGen.py`             | Ollama → JSON tag extraction                                  |
| `Validator_UDT.py`            | Name sanitization, type validation, BOOL array sizing         |
| `L5XGen_UDT.py`               | L5X XML generation, member sort key, type sizes               |
| `L5XOpt_UDT.py`               | UDT optimization, alignment math, full-program rewrite        |
| `Attach_L5Xanalyzer.py`       | Detects L5X target type (DataType / Controller)               |
| `Attach_UDTVerify.py`         | Issue analysis for a single attached UDT                      |
| `Attach_ProcessL5XProgram.py` | Issue analysis + rewrite orchestration for full programs      |
| `Attach_ProcessCSV.py`        | CSV batch → zip of L5X                                        |
| `Attach_ProcessExcel.py`      | Excel batch → zip of L5X                                      |
| `Attach_UDTBatch.py`          | Shared batch zip builder                                      |
| `test_core.py`                | Regression test suite                                         |

---

## Known limitations

- **Single-process only.** State (`stored_files`, `known_udts`, `active_model`) lives in module globals. Multi-worker deployment would need a session store.
- **AOI size estimation** only considers Parameters, not LocalTags. For AOIs with significant local storage the estimate will be low.
- **LLM output is best-effort.** If the model returns malformed JSON the request fails cleanly with a hint to rephrase — there's no retry loop.

