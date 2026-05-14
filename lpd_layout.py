#!/usr/bin/env python3
"""
lpd_layout.py — Auto-layout organizer for Infor IDP/IPA .lpd/.idp process files

Recomputes x/y coordinates for every activity node so the flow reads
cleanly left-to-right, with parallel branch paths on separate horizontal rows.

Usage:
  python lpd_layout.py <file.lpd>                      # layout + auto-backup
  python lpd_layout.py <file.lpd> --preview            # show new coords, don't write
  python lpd_layout.py <file.lpd> --restore            # restore most recent backup
  python lpd_layout.py <file.lpd> --col-width 220      # custom column width
  python lpd_layout.py <file.lpd> --row-height 150     # custom row height

Requirements: Python 3.6+ (stdlib only, no pip installs needed)
"""

import argparse
import math
import os
import sys
from collections import defaultdict, deque

from lpd_common import (
    parse_lpd, build_graph, topological_sort,
    validate_refs as validate,
    make_backup, find_latest_backup, restore_backup, write_lpd,
    ITERATOR_TYPES,
)

# ── Layout constants (overridable via CLI) ────────────────────────────────────
COL_WIDTH  = 160   # pixels between columns  (horizontal spacing)
ROW_HEIGHT = 100   # pixels between parallel rows (vertical spacing)
BAND_GAP   = 80    # fixed extra gap between bands (vertical)
START_X    = 40    # left margin
START_Y    = 80    # top margin (main flow anchored here)
MAX_COLS   = 0     # 0 = auto-detect (targets ~3 bands); override with --max-cols or --bands


# ── Branch analysis ───────────────────────────────────────────────────────────

def branch_exclusive_counts(branch_targets, out_edges):
    """
    For each branch target, count nodes reachable ONLY from it (exclusive descendants).
    The target with the most exclusive nodes is the "main" (heaviest) path.
    """
    def bfs(start):
        visited, q = set(), deque([start])
        while q:
            n = q.popleft()
            if n in visited:
                continue
            visited.add(n)
            for (dst, _) in out_edges.get(n, []):
                q.append(dst)
        return visited

    reachable = {t: bfs(t) for t in branch_targets}
    result = {}
    for t in branch_targets:
        others = set().union(*(v for k, v in reachable.items() if k != t))
        result[t] = len(reachable[t] - others)
    return result


def find_main_branch_targets(node_ids, out_edges, activities):
    """
    For each BRANCH node, identify which outgoing target is the "main" branch
    (most exclusive descendants → stays on current row, continues right).
    Returns {branch_node_id: main_target_id}.
    """
    main = {}
    for n in node_ids:
        if activities[n].get('activityType') != 'BRANCH':
            continue
        targets = [dst for dst, t in out_edges.get(n, []) if t == 'BRANCH']
        if not targets:
            continue
        if len(targets) == 1:
            main[n] = targets[0]
        else:
            excl = branch_exclusive_counts(targets, out_edges)
            main[n] = max(targets, key=lambda t: excl.get(t, 0))
    return main


# ── Column assignment ─────────────────────────────────────────────────────────

def assign_columns(node_ids, out_edges, in_edges, activities):
    """
    Longest-path column assignment with branch-entry alignment:
    - Main branch target (heaviest exclusive path) → col N+1  (continues right)
    - Side branch targets                          → col N    (same X as BRANCH)

    The side-branch rule makes BRANCH→side_target edges draw as vertical lines
    in IDP Studio (same x-coordinate, different y).
    """
    topo = topological_sort(node_ids, out_edges, in_edges)
    main_target = find_main_branch_targets(node_ids, out_edges, activities)
    branch_nodes = set(main_target.keys())

    cols = {n: 0 for n in node_ids}
    for n in topo:
        for (dst, edge_type) in out_edges.get(n, []):
            if edge_type == 'BRANCH' and n in branch_nodes:
                # Main branch continues right; side branches start at same column
                candidate = cols[n] + 1 if main_target.get(n) == dst else cols[n]
            else:
                candidate = cols[n] + 1
            if candidate > cols.get(dst, 0):
                cols[dst] = candidate

    return cols, topo, main_target


# ── Row assignment ────────────────────────────────────────────────────────────

def assign_rows(node_ids, out_edges, in_edges, activities, main_target, topo_order):
    """
    Topological-order row assignment (guarantees predecessors are resolved first).

    Rules:
    - Root nodes (no predecessors): row 0.
    - BRANCH node → pre-assigns rows to all its branch targets before they are
      processed: main target inherits current row, side targets get a new row below.
    - Pure error handlers (ALL incoming edges are ERROR type) → bumped to a new
      row below the main flow so they never overlap with End or other row-0 nodes.
    - All other nodes: min(predecessor rows) ignoring ERROR-edge predecessors.
      Merge points naturally snap back to the lowest row.
    """
    rows = {}
    max_row = [0]
    pending = {}   # node_id -> row pre-assigned by a BRANCH node above it

    for n in topo_order:
        preds = in_edges.get(n, [])

        if not preds:
            rows[n] = 0
        else:
            # Separate error-edge preds from structural (normal/branch/iter) preds
            non_error_preds = [(p, t) for p, t in preds if t != 'ERROR']

            if not non_error_preds:
                # Pure error handler — give it a dedicated row below main flow
                if n in pending:
                    rows[n] = pending[n]
                else:
                    max_row[0] += 1
                    rows[n] = max_row[0]
            else:
                # Use only structural predecessors to determine row
                non_branch_rows = [rows.get(p, 0) for p, t in non_error_preds if t != 'BRANCH']
                if n in pending:
                    if non_branch_rows:
                        rows[n] = min(pending[n], min(non_branch_rows))
                    else:
                        rows[n] = pending[n]
                else:
                    rows[n] = min(rows.get(p, 0) for p, _ in non_error_preds)

        max_row[0] = max(max_row[0], rows[n])

        # Pre-assign rows to branch targets immediately
        if activities[n].get('activityType') == 'BRANCH':
            branch_targets = [dst for dst, t in out_edges.get(n, []) if t == 'BRANCH']
            main = main_target.get(n)
            for dst in branch_targets:
                if dst == main:
                    pending[dst] = rows[n]      # main continues on same row
                else:
                    max_row[0] += 1
                    pending[dst] = max_row[0]   # side gets next available row

    for n in node_ids:
        if n not in rows:
            rows[n] = 0

    return rows


# ── Flat mode: align branch returns ──────────────────────────────────────────

def align_branch_returns(cols, rows, in_edges):
    """
    For each merge point (node with 2+ predecessors), move any side-branch
    predecessor (row > merge row) to the merge point's column.

    This makes the return edge from the last side-branch node to the merge
    point draw as a vertical line in IDP Studio (same x, different y).
    Combined with branch-entry alignment (vertical entry edges), the result
    is a rectangular bracket shape for each branch:

        BRANCH ──→ D ──→ E ──→ MERGE ──→ ...
          ↓                      ↑
          C ─────────────────────┘   (C is on row below; entry+return are vertical)
    """
    for n in list(cols.keys()):
        preds = in_edges.get(n, [])
        if len(preds) <= 1:
            continue
        merge_row = rows.get(n, 0)
        merge_col = cols[n]
        for pred, _edge_type in preds:
            if rows.get(pred, 0) > merge_row:
                cols[pred] = merge_col  # same X as merge point → vertical return edge


# ── Crossing reduction (barycenter heuristic) ────────────────────────────────

def reduce_crossings(cols, rows, out_edges, in_edges, iterations=20):
    """
    Barycenter heuristic crossing reduction (Sugiyama framework, phase 3).

    Runs alternating forward/backward passes over columns. Forward passes use
    predecessor rows as the barycenter reference; backward passes use successor
    rows. This directional separation is the key improvement over using both
    sides simultaneously — it mirrors the two-sided sweep used by Graphviz and
    ELK (which run 15-20 passes; 4 was too few for complex branchy processes).

    The same set of row values stays in use for each column — nodes are
    reordered within their column, never assigned new row numbers.

    Always runs (no flag needed). 20 iterations is fast even for 184-node flows.
    """
    col_nodes = defaultdict(list)
    for n in cols:
        col_nodes[cols[n]].append(n)

    col_order = sorted(col_nodes.keys())

    for iteration in range(iterations):
        # Alternate direction: even = forward (left-to-right), odd = backward
        sweep = col_order if iteration % 2 == 0 else reversed(col_order)
        changed = False

        for c in sweep:
            nodes_in_col = col_nodes[c]
            if len(nodes_in_col) <= 1:
                continue

            barycenters = {}
            for n in nodes_in_col:
                if iteration % 2 == 0:
                    # Forward pass: pull toward predecessors (nodes to the left)
                    neighbor_rows = [rows[p] for p, _ in in_edges.get(n, []) if p in rows]
                else:
                    # Backward pass: pull toward successors (nodes to the right)
                    neighbor_rows = [rows[s] for s, _ in out_edges.get(n, []) if s in rows]

                # Fall back to both sides if one is empty (isolated or end node)
                if not neighbor_rows:
                    neighbor_rows = (
                        [rows[p] for p, _ in in_edges.get(n, [])  if p in rows] +
                        [rows[s] for s, _ in out_edges.get(n, []) if s in rows]
                    )
                barycenters[n] = sum(neighbor_rows) / len(neighbor_rows) if neighbor_rows else rows[n]

            sorted_nodes = sorted(nodes_in_col, key=lambda n: (barycenters[n], rows[n]))
            sorted_rows  = sorted(rows[n] for n in nodes_in_col)
            for n, r in zip(sorted_nodes, sorted_rows):
                if rows[n] != r:
                    changed = True
                rows[n] = r

        if not changed:
            break

    return rows


# ── Spring layout (Y-axis only, hierarchical spring) ─────────────────────────

def spring_layout(cols, out_edges, in_edges, col_width, row_height,
                  iterations=150, learning_rate=0.35):
    """
    Hierarchical spring layout: X is fixed to topological column (preserves
    left-to-right flow direction), Y is found by spring relaxation.

    Algorithm:
      1. Initialize Y: stagger nodes within each column to break symmetry
      2. Spring relaxation (iterations passes):
           For each node n:
             target_y = mean Y of all connected neighbors
             y[n] += learning_rate * (target_y - y[n])
           Decay learning_rate slightly each step to stabilize
      3. Snap Y to nearest row_height grid
      4. Resolve same-column collisions: sort by snapped row, assign
         consecutive rows where needed

    Pure attraction (no repulsion during simulation) keeps the maths stable.
    Collision resolution at the end spreads same-column nodes apart.
    Connected nodes cluster together; unconnected subtrees separate naturally.
    """
    col_nodes = defaultdict(list)
    for n in cols:
        col_nodes[cols[n]].append(n)

    # Initialize: stagger within each column so connected nodes have somewhere to pull from
    y_pos = {}
    for c, nodes in col_nodes.items():
        for i, n in enumerate(nodes):
            y_pos[n] = float(i * row_height)

    # Spring relaxation: pull each node toward mean Y of its neighbors
    lr = learning_rate
    for _ in range(iterations):
        new_y = {}
        for n in cols:
            neighbors = (
                [p for p, _ in in_edges.get(n, [])  if p in y_pos] +
                [s for s, _ in out_edges.get(n, []) if s in y_pos]
            )
            if neighbors:
                target = sum(y_pos[nb] for nb in neighbors) / len(neighbors)
                new_y[n] = y_pos[n] + lr * (target - y_pos[n])
            else:
                new_y[n] = y_pos[n]
        y_pos = new_y
        lr *= 0.99   # Slow decay to prevent oscillation

    # Snap Y to nearest row grid
    snapped = {n: round(y / row_height) for n, y in y_pos.items()}

    # Resolve same-column collisions: sort by final Y, assign consecutive rows if dupes
    rows = {}
    for c, nodes in col_nodes.items():
        order = sorted(nodes, key=lambda n: (snapped[n], n))
        assigned = []
        for n in order:
            r = snapped[n]
            while r in assigned:
                r += 1
            assigned.append(r)
            rows[n] = r

    return rows


# ── Band (wrap) helpers ───────────────────────────────────────────────────────

def resolve_target_cols(max_cols_arg, bands_arg, no_wrap_arg, total_cols):
    """
    Return the target columns-per-band.
    Priority: --no-wrap > --bands > --max-cols > auto (~3 bands, min 8 cols).
    """
    if no_wrap_arg:
        return max(total_cols, 1)
    if bands_arg and bands_arg > 0:
        return max(1, math.ceil(total_cols / bands_arg))
    if max_cols_arg and max_cols_arg > 0:
        return max_cols_arg
    return max(8, math.ceil(total_cols / 3))


def find_safe_columns(cols, out_edges, activities, total_cols):
    """
    A column is 'safe' for band wrapping if no open BRANCH section spans it.
    For each BRANCH node we find its merge column (first common descendant of
    all branch targets). Any column strictly between branch_col and merge_col
    is unsafe.
    """
    branch_nodes = [n for n in cols if activities[n].get('activityType') == 'BRANCH']

    unsafe_spans = []   # list of (branch_col, merge_col) half-open intervals [b, m)
    for bn in branch_nodes:
        targets = [dst for dst, t in out_edges.get(bn, []) if t == 'BRANCH']
        if len(targets) < 2:
            continue

        # BFS reachable set from each branch target
        def bfs(start):
            visited, q = set(), deque([start])
            while q:
                n = q.popleft()
                if n in visited:
                    continue
                visited.add(n)
                for (dst, _) in out_edges.get(n, []):
                    q.append(dst)
            return visited

        reachable = [bfs(t) for t in targets]
        common = reachable[0]
        for r in reachable[1:]:
            common = common & r

        if common:
            merge_col = min(cols.get(n, 0) for n in common)
            unsafe_spans.append((cols[bn], merge_col))

    safe = set()
    for c in range(total_cols):
        is_safe = not any(bc <= c < mc for bc, mc in unsafe_spans)
        if is_safe:
            safe.add(c)
    return safe


def assign_bands(cols, rows, out_edges, activities, target_cols):
    """
    Assign each column to a band, wrapping ONLY at columns that are safe
    (not inside any open BRANCH section).
    Returns (col_to_band, band_start_col) dicts.
    """
    total_cols = max(cols.values(), default=0) + 1
    safe = find_safe_columns(cols, out_edges, activities, total_cols)

    col_to_band = {}
    band_start = {0: 0}
    band = 0
    cols_in_band = 0

    for c in range(total_cols):
        col_to_band[c] = band
        cols_in_band += 1
        next_c = c + 1
        if next_c < total_cols and next_c in safe:
            if cols_in_band >= target_cols:
                band += 1
                band_start[band] = next_c
                cols_in_band = 0

    return col_to_band, band_start


def compute_band_offsets(cols, rows, col_to_band, row_height, band_gap):
    """
    Compute cumulative Y offsets for each band.
    Band height = (max_row_in_band + 1) * row_height + band_gap.
    """
    max_row_per_band = defaultdict(int)
    for n in cols:
        b = col_to_band.get(cols[n], 0)
        max_row_per_band[b] = max(max_row_per_band[b], rows.get(n, 0))

    num_bands = max(col_to_band.values(), default=0) + 1
    offsets = {}
    cumulative = 0
    for b in range(num_bands):
        offsets[b] = cumulative
        cumulative += (max_row_per_band[b] + 1) * row_height + band_gap
    return offsets


def node_pixels(node_id, cols, rows, col_width, row_height,
                col_to_band, band_start, band_offsets, start_x, start_y):
    """Return (x, y) pixel position for a node."""
    c = cols[node_id]
    r = rows[node_id]
    b = col_to_band.get(c, 0)
    x = start_x + (c - band_start.get(b, 0)) * col_width
    y = start_y + band_offsets.get(b, 0) + r * row_height
    return x, y


# ── Main layout routine ───────────────────────────────────────────────────────

def layout(filepath, col_width, row_height, band_gap, max_cols_arg, bands_arg, no_wrap,
           start_x, start_y, flat=False, spring=False, preview=False):
    print(f"Parsing: {filepath}")
    tree, root, activities, edges = parse_lpd(filepath)
    node_ids = list(activities.keys())

    if not node_ids:
        print("No activity nodes found -- nothing to do.")
        return False

    # Validate before touching anything
    pre_errors = validate(root, set(node_ids))
    if pre_errors:
        print("WARNING: File has pre-existing validation errors:")
        for e in pre_errors:
            print(f"  {e}")

    out_edges, in_edges = build_graph(activities, edges)
    cols, topo_order, main_target = assign_columns(node_ids, out_edges, in_edges, activities)
    total_cols = max(cols.values(), default=0) + 1

    if spring:
        # Spring mode: Y determined by spring simulation, no band wrapping
        rows = spring_layout(cols, out_edges, in_edges, col_width, row_height)
        max_row = max(rows.values(), default=0)
        canvas_w = start_x + total_cols * col_width
        canvas_h = start_y + (max_row + 1) * row_height
        print(f"  Nodes: {len(node_ids)}  |  Columns: {total_cols}  |  Spring rows: {max_row+1}  |  1 band (no wrap)")
        print(f"  Canvas: ~{canvas_w} x {canvas_h} px")

        if preview:
            print(f"\n  Preview (id -> col / row -> x, y):")
            print(f"  {'ID':<35} {'COL':>4} {'ROW':>4}   {'X':>5} {'Y':>5}")
            print(f"  {'-'*35} {'-'*4} {'-'*4}   {'-'*5} {'-'*5}")
            for n in topo_order:
                x = start_x + cols[n] * col_width
                y = start_y + rows[n] * row_height
                print(f"  {n:<35} {cols[n]:>4} {rows[n]:>4}   {x:>5} {y:>5}")
            return True

        for n, act in activities.items():
            act.set('x', str(start_x + cols[n] * col_width))
            act.set('y', str(start_y + rows[n] * row_height))

    else:
        # Standard hierarchical mode
        rows = assign_rows(node_ids, out_edges, in_edges, activities, main_target, topo_order)

        # Flat mode: pull each side-branch's last node to the merge column
        if flat:
            align_branch_returns(cols, rows, in_edges)
            no_wrap = True   # flat always uses a single horizontal band

        # Crossing reduction: always run (20 alternating passes)
        reduce_crossings(cols, rows, out_edges, in_edges)

        target_cols = resolve_target_cols(max_cols_arg, bands_arg, no_wrap, total_cols)
        col_to_band, band_start = assign_bands(cols, rows, out_edges, activities, target_cols)
        band_offsets            = compute_band_offsets(cols, rows, col_to_band, row_height, band_gap)

        max_row   = max(rows.values(), default=0)
        num_bands = max(col_to_band.values(), default=0) + 1
        max_col_in_band = max(
            (cols[n] - band_start.get(col_to_band.get(cols[n], 0), 0) for n in cols),
            default=0
        )
        canvas_w = start_x + (max_col_in_band + 1) * col_width
        canvas_h = start_y + band_offsets.get(num_bands - 1, 0) + (max_row + 1) * row_height

        band_desc = f"{num_bands} band{'s' if num_bands != 1 else ''} of ~{target_cols} cols"
        print(f"  Nodes: {len(node_ids)}  |  Columns: {total_cols}  |  Parallel rows: {max_row+1}  |  {band_desc}")
        print(f"  Canvas: ~{canvas_w} x {canvas_h} px")

        if preview:
            print(f"\n  Preview (id -> col / band / row -> x, y):")
            print(f"  {'ID':<35} {'COL':>4} {'BND':>4} {'ROW':>4}   {'X':>5} {'Y':>5}")
            print(f"  {'-'*35} {'-'*4} {'-'*4} {'-'*4}   {'-'*5} {'-'*5}")
            for n in topo_order:
                x, y = node_pixels(n, cols, rows, col_width, row_height,
                                   col_to_band, band_start, band_offsets, start_x, start_y)
                print(f"  {n:<35} {cols[n]:>4} {col_to_band.get(cols[n],0):>4} {rows[n]:>4}   {x:>5} {y:>5}")
            return True

        for n, act in activities.items():
            x, y = node_pixels(n, cols, rows, col_width, row_height,
                               col_to_band, band_start, band_offsets, start_x, start_y)
            act.set('x', str(x))
            act.set('y', str(y))

    # Validate after layout
    post_errors = validate(root, set(node_ids))
    if post_errors:
        print("ERROR: Validation failed after layout -- aborting write.")
        for e in post_errors:
            print(f"  {e}")
        return False

    return tree


def main():
    parser = argparse.ArgumentParser(
        description='Auto-layout Infor IDP/IPA .lpd process files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Layout style (mutually exclusive -- pick one or use auto default):
  --flat          Single row; error/branch paths drop below and return vertically
  --no-wrap       Single horizontal row, no wrapping
  --bands N       Target N horizontal bands (e.g. --bands 2)
  --max-cols N    Exact columns per band before wrapping

Spacing:
  --col-width N   Horizontal spacing in pixels  (default: 160)
  --row-height N  Vertical spacing in pixels    (default: 100)
  --band-gap N    Extra gap between bands       (default: 80)
  --start-x N     Left canvas margin in pixels  (default: 40)
  --start-y N     Top canvas margin in pixels   (default: 80)

Examples:
  python lpd_layout.py MyProcess.lpd                   # auto layout + backup
  python lpd_layout.py MyProcess.lpd --preview         # show result without writing
  python lpd_layout.py MyProcess.lpd --restore         # undo last layout
  python lpd_layout.py MyProcess.lpd --flat            # cleanest for branchy flows
  python lpd_layout.py MyProcess.lpd --spring          # spring Y layout for dense/complex flows
  python lpd_layout.py MyProcess.lpd --bands 2         # force 2 horizontal bands
  python lpd_layout.py MyProcess.lpd --no-wrap         # single long row
  python lpd_layout.py MyProcess.lpd --col-width 200 --row-height 120
        """
    )
    parser.add_argument('file', help='.lpd or .idp file to layout')

    # Modes
    parser.add_argument('--preview', action='store_true',
                        help='Print new coordinates without writing the file')
    parser.add_argument('--restore', action='store_true',
                        help='Restore the most recent backup for this file')

    # Wrap control (mutually exclusive intent, last one wins in resolve_max_cols)
    wrap = parser.add_mutually_exclusive_group()
    wrap.add_argument('--bands',    type=int, metavar='N', default=0,
                      help='Target N horizontal bands of flow (auto-computes column wrap point)')
    wrap.add_argument('--max-cols', type=int, metavar='N', default=0,
                      help='Exact columns per band before wrapping (default: auto ~3 bands)')
    wrap.add_argument('--no-wrap',  action='store_true',
                      help='No wrapping -- lay out in one long horizontal row')
    wrap.add_argument('--flat',     action='store_true',
                      help='Single row; side branches drop below and return vertically (rectangular bracket shape)')
    wrap.add_argument('--spring',   action='store_true',
                      help='Spring layout: Y positions found by edge-attraction simulation; nodes cluster by connectivity. Best for complex processes with many branches.')

    # Spacing
    parser.add_argument('--col-width',  type=int, default=COL_WIDTH,  metavar='N',
                        help=f'Horizontal spacing between columns in px (default: {COL_WIDTH})')
    parser.add_argument('--row-height', type=int, default=ROW_HEIGHT, metavar='N',
                        help=f'Vertical spacing between parallel rows in px (default: {ROW_HEIGHT})')
    parser.add_argument('--band-gap',   type=int, default=BAND_GAP,   metavar='N',
                        help=f'Extra vertical gap between bands in px (default: {BAND_GAP})')

    # Canvas origin
    parser.add_argument('--start-x', type=int, default=START_X, metavar='N',
                        help=f'Left canvas margin in px (default: {START_X})')
    parser.add_argument('--start-y', type=int, default=START_Y, metavar='N',
                        help=f'Top canvas margin in px (default: {START_Y})')

    args = parser.parse_args()

    filepath = args.file
    if not os.path.isfile(filepath):
        print(f"File not found: {filepath}")
        sys.exit(1)

    # ── Restore mode ──────────────────────────────────────────────────────────
    if args.restore:
        sys.exit(0 if restore_backup(filepath) else 1)

    # ── Preview mode ──────────────────────────────────────────────────────────
    if args.preview:
        layout(filepath,
               col_width=args.col_width, row_height=args.row_height, band_gap=args.band_gap,
               max_cols_arg=args.max_cols, bands_arg=args.bands, no_wrap=args.no_wrap,
               start_x=args.start_x, start_y=args.start_y,
               flat=args.flat, spring=args.spring, preview=True)
        sys.exit(0)

    # ── Layout + write ────────────────────────────────────────────────────────
    backup_path = make_backup(filepath)
    print(f"Backup:  {backup_path}")

    result = layout(filepath,
                    col_width=args.col_width, row_height=args.row_height, band_gap=args.band_gap,
                    max_cols_arg=args.max_cols, bands_arg=args.bands, no_wrap=args.no_wrap,
                    start_x=args.start_x, start_y=args.start_y,
                    flat=args.flat, spring=args.spring, preview=False)

    if result is False:
        print("Layout failed. Original file unchanged (backup exists at above path).")
        sys.exit(1)
    if result is True:
        sys.exit(0)

    write_lpd(result, filepath)
    print(f"Done.    {filepath}")
    print(f"Tip:     To undo -> python lpd_layout.py \"{filepath}\" --restore")


if __name__ == '__main__':
    main()