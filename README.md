# lpd-tools

Command-line toolkit for **Infor IDP / IPA** process files (`.lpd`).

Infor Process Designer has no built-in auto-arrange, no diff view, no cross-process search, and no structural validator beyond what IDP surfaces at load time. These tools fill those gaps.

---

## Tools

| Script | What it does |
|--------|-------------|
| [`lpd_layout.py`](#lpd_layout--auto-arrange-node-coordinates) | Auto-arrange node x/y coordinates so the flow reads left-to-right |
| [`lpd_validate.py`](#lpd_validate--structural-validator) | Validate process structure — catches errors before IDP does |
| [`lpd_diff.py`](#lpd_diff--compare-two-versions) | Compare two versions of a process — see exactly what changed |
| [`lpd_search.py`](#lpd_search--cross-process-search) | Search across a folder of LPD files by node type, prop, or text |
| [`lpd_rename.py`](#lpd_rename--rename-a-node-id) | Rename a node ID and update all references safely |
| [`lpd_export.py`](#lpd_export--diagram-export) | Export a process to Graphviz DOT or draw.io diagram format |
| `lpd_common.py` | Shared utilities (imported by other scripts — not a standalone tool) |

---

## Requirements

- **Python 3.6+** — no third-party packages needed (stdlib only)
- All scripts must be in the **same folder** (they import `lpd_common.py`)

```bash
git clone https://github.com/JazzPiece/infor-lpd-tools.git
cd infor-lpd-tools
python lpd_layout.py MyProcess.lpd
```

---

## lpd_layout — Auto-arrange node coordinates

Recomputes x/y for every activity node so the flow reads cleanly left-to-right,
with branch paths on separate rows and long flows wrapping into horizontal bands.

### Quick start

```bash
python lpd_layout.py MyProcess.lpd              # layout + auto backup
python lpd_layout.py MyProcess.lpd --preview    # preview only, nothing written
python lpd_layout.py MyProcess.lpd --restore    # undo last layout
python lpd_layout.py MyProcess.lpd --flat       # cleanest for branchy flows
python lpd_layout.py --dir ./production/        # layout every .lpd in a folder
python lpd_layout.py --dir ./production/ --preview   # preview all, nothing written
python lpd_layout.py --dir ./production/ --restore   # restore all backups in a folder
```

### All flags

**Mode**

| Flag | What it does |
|------|-------------|
| _(none)_ | Layout and write (backup created first) |
| `--preview` | Print new coordinates, don't write |
| `--restore` | Restore most recent backup for this file |
| `--dir DIR` | Batch mode — layout every `.lpd` in `DIR` |

**Layout style** _(mutually exclusive)_

| Flag | What it does |
|------|-------------|
| _(auto)_ | Wrap into ~3 horizontal bands for a balanced canvas |
| `--flat` | Single row; branch/error paths drop below and return vertically |
| `--no-wrap` | One long horizontal row, no wrapping |
| `--bands N` | Force exactly N horizontal bands |
| `--max-cols N` | Wrap after exactly N columns |
| `--spring` | Spring layout: Y positions found by edge-attraction simulation. Connected nodes cluster together vertically. Best for complex processes with many branches where the standard grid becomes cluttered. No band wrapping — one wide canvas. |

> `--flat` gives the cleanest result for most processes.
> `--spring` is best for large, dense processes (50+ nodes, many branches) where you want natural clustering over a rigid grid.

**Crossing reduction**

| Flag | What it does |
|------|-------------|
**Spacing**

| Flag | Default | What it does |
|------|---------|-------------|
| `--col-width N` | `160` | Pixels between columns |
| `--row-height N` | `100` | Pixels between rows |
| `--band-gap N` | `80` | Extra gap between bands |
| `--start-x N` | `40` | Left margin |
| `--start-y N` | `80` | Top margin |

### Output example

```
Parsing: MyProcess.lpd
Backup:  MyProcess_layout_backup_20260327_091523.lpd
  Nodes: 34  |  Columns: 18  |  Parallel rows: 2  |  3 bands of ~6 cols
  Canvas: ~1000 x 360 px
Done.    MyProcess.lpd
Tip:     To undo -> python lpd_layout.py "MyProcess.lpd" --restore
```

### How it works

| Phase | Algorithm |
|-------|-----------|
| Column (X) | Longest-path relaxation on topological order |
| Row (Y) | Topological order (Kahn's) + pending pre-assignment for BRANCH nodes |
| Main branch detection | BFS — exclusive reachable count per target |
| Safe wrap columns | BFS — common descendants (merge point) |
| Crossing reduction | Directional barycenter, 20 alternating Sugiyama passes — always runs |

Error handler nodes (nodes whose only incoming edges are ERROR type) are automatically placed on a dedicated row below the main flow so they never overlap with End or other row-0 nodes.

---

## lpd_validate — Structural validator

Catches structural problems that IDP only surfaces at load time (or silently ignores).
Run before every deploy.

### Usage

```bash
python lpd_validate.py MyProcess.lpd                  # validate one file
python lpd_validate.py file1.lpd file2.lpd            # validate multiple
python lpd_validate.py --dir ./production/            # validate entire folder
python lpd_validate.py MyProcess.lpd --strict         # treat warnings as errors
```

### Checks

| Severity | Check |
|----------|-------|
| ERROR | Edge `from`/`to` references a missing node |
| ERROR | `OnActivityError` goto references a missing node |
| ERROR | Duplicate activity IDs |
| ERROR | Missing ItEnd pair for iterator (QUERY, LM, ITERFR, LOOP, etc.) |
| ERROR | BRANCH node with non-BRANCH outgoing edges |
| ERROR | Blocked node type used (CUSAD, SQLUP, SQLQR) |
| ERROR | No START node / no END node / multiple START nodes |
| WARN | Error-path END node missing `processStatus=ERROR` |
| WARN | Hardcoded email address instead of `<!_configuration.*>` token |
| WARN | Empty required field (`to` on EMAIL, `callString` on WEBRN, etc.) |

### Output example

```
Validating: MyProcess.lpd  (34 nodes)
  ERROR  GetEmployee1000 (LM): missing paired ItEnd node (expected id='End-GetEmployee1000')
  WARN   SendEmail3000 (EMAIL): required field 'content' (message body) is empty  -  configure in IDP

1 error, 1 warning.
```

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Clean (no errors; warnings allowed unless `--strict`) |
| `1` | One or more errors found |

---

## lpd_diff — Compare two versions

Shows what changed between an original and modified version of a process.
Useful on change-window days to verify exactly what was modified before deploy.

### Usage

```bash
python lpd_diff.py original.lpd new.lpd
python lpd_diff.py original.lpd new.lpd --brief       # summary counts only
python lpd_diff.py original.lpd new.lpd --no-coords   # skip x/y changes
```

### Output example

```
Diff: OriginalProcess.lpd  ->  NewProcess.lpd

ADDED nodes (1):
  + CheckExistingAccount5000 (QUERY)

REMOVED nodes (1):
  - OldNode3000 (LM)

CHANGED nodes (1):
  ~ SendReport8500 (EMAIL)
      to: 'hr@example.org' -> '<!_configuration.toEmail_InforHRAnalysts.toEmail>'

ADDED edges (2):
  + NORMAL  CheckExistingAccount5000 -> BranchIfNew6000
  + BRANCH  BranchIfNew6000 -> SkipExisting5500

REMOVED edges (1):
  - NORMAL  AssignFields2000 -> OldNode3000

1 node(s) added,  1 node(s) removed,  1 node(s) changed,  2 edge(s) added,  1 edge(s) removed.
```

> Tip: Use `--no-coords` after running `lpd_layout.py` to filter out pure coordinate changes
> and see only structural/prop differences.

---

## lpd_search — Cross-process search

Search across a folder of LPD files for nodes, prop values, text, or audit conditions.
Useful for impact analysis before changes and org-wide audits.

### Usage

```bash
python lpd_search.py <dir> --node WEBRN
python lpd_search.py <dir> --prop to "reports@"
python lpd_search.py <dir> --text "VendorImport"
python lpd_search.py <dir> --hardcoded-email
python lpd_search.py <dir> --no-error-handler
python lpd_search.py <dir> --files-only            # filenames only, no detail
```

Multiple flags combine with AND logic (file must match all criteria):
```bash
python lpd_search.py ./production/ --node LM --prop transactionString "Employee"
```

### Search flags

| Flag | What it finds |
|------|--------------|
| `--node TYPE` | Files containing a node of this `activityType` |
| `--prop NAME VALUE` | Files where prop `NAME` contains `VALUE` substring |
| `--text STRING` | Files where any CDATA content contains `STRING` |
| `--hardcoded-email` | EMAIL nodes with raw addresses instead of `<!_configuration.*>` tokens |
| `--no-error-handler` | Processes with no error handler node and no ERROR edges |
| `--files-only` | Suppress match details, show file names only |

### Output example

```
Searching 125 file(s) for --node WEBRN ...

  BackgroundCheck_Acme.lpd  (1 match)
    - SubmitRequest (WEBRN) -- caption: 'SubmitRequest'

  Terminate_Acme.lpd  (1 match)
    - disableUser (WEBRN) -- caption: 'disableUser'

17 matches across 7 files.
```

---

## lpd_rename — Rename a node ID

Renames a node ID and updates every reference in the file:
the `id=` attribute, all `from=`/`to=` edges, and all `OnActivityError` goto targets.
Creates a backup before writing.

### Usage

```bash
python lpd_rename.py MyProcess.lpd OldNodeId NewNodeId
python lpd_rename.py MyProcess.lpd OldNodeId NewNodeId --preview
```

### Output example

```
NOTE: 'GetEmployee1000' is an iterator. Its paired ItEnd 'End-GetEmployee1000'
      should also be renamed to 'End-NewNodeId'.
      Run: python lpd_rename.py MyProcess.lpd End-GetEmployee1000 End-NewNodeId

Renaming 'GetEmployee1000' -> 'QueryActiveEmployees1000' in MyProcess.lpd

  activity id='GetEmployee1000'
  edge 0: 'Start' -> to='GetEmployee1000'
  edge 1: from='GetEmployee1000' -> 'AssignFields2000'
  OnActivityError goto in 'AssignFields2000' targets 'GetEmployee1000'

  4 references to update.

Backup:  MyProcess_rename_backup_20260327_091523.lpd
Done.    MyProcess.lpd
```

---

## lpd_export — Diagram export

Export a process to a visual diagram. No diagramming software required for either format.

### Formats

| Format | Flag | How to view |
|--------|------|-------------|
| Graphviz DOT | `--format dot` _(default)_ | Paste the `.dot` file into **https://dreampuf.github.io/GraphvizOnline** — no install needed. Or install [Graphviz](https://graphviz.org/download/) and use `--render png/svg/pdf`. |
| draw.io XML | `--format drawio` | Open the `.drawio` file at **https://app.diagrams.net** (free, no login) or in the [draw.io desktop app](https://github.com/jgraph/drawio-desktop/releases). From draw.io you can export to Visio `.vsdx`, PDF, PNG, or SVG via **File › Export As**. |

### Usage

```bash
python lpd_export.py MyProcess.lpd                    # write MyProcess.dot
python lpd_export.py MyProcess.lpd --format drawio    # write MyProcess.drawio
python lpd_export.py MyProcess.lpd -o diagram.drawio  # explicit output path
python lpd_export.py MyProcess.lpd --render png       # dot + render to .png (needs Graphviz)
python lpd_export.py --dir ./production/              # export all .lpd in a folder
python lpd_export.py --dir ./production/ --format drawio
```

### Visual legend (all formats)

| Colour | Node type |
|--------|-----------|
| Grey | START / END |
| Blue | Iterator (QUERY, LM, ITERFR, LOOP, DATAEX, …) |
| Gold / yellow | BRANCH (decision) |
| Salmon / red | EMAIL |
| Green | WEBRN (web service call) |
| White | Everything else |

| Edge style | Edge type |
|------------|-----------|
| Solid black | NORMAL |
| Dashed blue | BRANCH |
| Dashed red | ERROR |

### Notes

- Coordinates from `lpd_layout.py` are used when present; otherwise a simple left-to-right topo grid is applied automatically.
- `--render` requires [Graphviz](https://graphviz.org/download/) (`dot` on PATH) and only applies to `--format dot`.

---

## Tested on

- Infor Landmark / IDP version `9.1.0` (Landmark 2026)
- Multi-tenant hosted Infor
- 181 production processes (HCM + FSM)
- Processes from 16 nodes (simple) to 218 nodes (14 branches, 24 iterators)

---

## License

MIT
