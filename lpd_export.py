#!/usr/bin/env python3
"""
lpd_export.py — Export Infor IDP/IPA .lpd process files to diagram formats

Supported formats:
  dot     Graphviz DOT — render locally with `dot` or paste into the online
          renderer at https://dreampuf.github.io/GraphvizOnline (no install needed)
  drawio  draw.io / diagrams.net XML — open in https://app.diagrams.net or the
          desktop app (File → Open); export to Visio .vsdx, PDF, PNG from there

Node colour legend (consistent across all formats):
  grey        START / END
  light blue  Iterator nodes (QUERY, LM, ITERFR, LOOP, DATAEX, LDAPQ, RMQR, IONIN, FORMTXN)
  gold        BRANCH nodes
  salmon/red  EMAIL nodes
  green       WEBRN (REST/web service calls)
  white       Everything else

Edge style legend:
  black solid    NORMAL
  blue dashed    BRANCH
  red dashed     ERROR

Usage:
  python lpd_export.py <file.lpd>                      # write <file>.dot (default)
  python lpd_export.py <file.lpd> --format drawio      # write <file>.drawio
  python lpd_export.py <file.lpd> -o out.drawio        # format inferred from extension
  python lpd_export.py <file.lpd> --render png         # .dot + render to .png (needs Graphviz)
  python lpd_export.py --dir ./production/             # export all .lpd in a folder
  python lpd_export.py --dir ./production/ --format drawio

Tip: run lpd_layout.py on your file first so node positions carry over into the diagram.

Requirements: Python 3.6+ (stdlib only). lpd_common.py must be in the same folder.
For --render: Graphviz must be installed and `dot` on PATH.
"""

import argparse
import glob as glob_module
import os
import shutil
import subprocess
import sys
from collections import defaultdict

from lpd_common import parse_lpd, build_graph, topological_sort, ITERATOR_TYPES


# ── Shared node/edge classification ──────────────────────────────────────────

# Each entry: (dot_fill, dot_shape, drawio_style)
_NODE_STYLES = {
    'START': (
        'grey85', 'oval',
        'ellipse;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;',
    ),
    'END': (
        'grey85', 'oval',
        'ellipse;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;',
    ),
    'BRANCH': (
        'gold', 'diamond',
        'rhombus;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;',
    ),
    'EMAIL': (
        'lightsalmon', 'box',
        'rounded=1;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;',
    ),
    'WEBRN': (
        'lightgreen', 'box',
        'rounded=1;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;',
    ),
}
_ITER_STYLES = (
    'lightblue', 'box',
    'rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;',
)
_DEFAULT_STYLES = (
    'white', 'box',
    'rounded=1;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=#000000;',
)

# Each entry: (dot_attr, drawio_style)
_EDGE_STYLES = {
    'NORMAL': ('color=black',             'edgeStyle=orthogonalEdgeStyle;html=1;'),
    'BRANCH': ('color=blue style=dashed', 'edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#0000FF;dashed=1;'),
    'ERROR':  ('color=red  style=dashed', 'edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#FF0000;dashed=1;'),
}


def _node_styles(activity_type):
    if activity_type in _NODE_STYLES:
        return _NODE_STYLES[activity_type]
    if activity_type in ITERATOR_TYPES:
        return _ITER_STYLES
    return _DEFAULT_STYLES


# ── Coordinate helper ─────────────────────────────────────────────────────────

_NODE_W = 120   # node box width  in pixels
_NODE_H = 40    # node box height in pixels


def _get_coords(activities, edges_list):
    """
    Return {node_id: (cx, cy)} treating .lpd x/y as the node centre.
    Falls back to a simple topo-order grid when most nodes are at (0, 0),
    i.e. when lpd_layout.py has not been run yet.
    """
    coords = {}
    for nid, act in activities.items():
        try:
            x = float(act.get('x') or 0)
            y = float(act.get('y') or 0)
        except ValueError:
            x, y = 0.0, 0.0
        coords[nid] = (x, y)

    at_origin = sum(1 for x, y in coords.values() if x == 0.0 and y == 0.0)
    if at_origin > max(1, len(coords) // 2):
        out_edges, in_edges = build_graph(activities, edges_list)
        topo = topological_sort(list(activities.keys()), out_edges, in_edges)
        col_map = {}
        for n in topo:
            col_map[n] = max(
                (col_map.get(p, 0) + 1 for p, _ in in_edges.get(n, [])),
                default=0,
            )
        col_nodes = defaultdict(list)
        for n in topo:
            col_nodes[col_map.get(n, 0)].append(n)
        COL_W, ROW_H, OX, OY = 160, 100, 80, 80
        for c, nodes in col_nodes.items():
            for r, n in enumerate(nodes):
                coords[n] = (OX + c * COL_W, OY + r * ROW_H)

    return coords


# ── DOT export ────────────────────────────────────────────────────────────────

def export_dot(filepath):
    """Return a Graphviz DOT string for the process. Raises on parse failure."""
    _, _, activities, edges_list = parse_lpd(filepath)
    out_edges, in_edges = build_graph(activities, edges_list)
    topo = topological_sort(list(activities.keys()), out_edges, in_edges)

    name = os.path.splitext(os.path.basename(filepath))[0]
    lines = [
        '// Paste into https://dreampuf.github.io/GraphvizOnline to render online (no install needed).',
        f'digraph "{name}" {{',
        '  rankdir=LR;',
        '  node [fontname="Helvetica" fontsize=10];',
        '  edge [fontsize=9];',
        '',
    ]

    for nid in topo:
        act = activities[nid]
        atype = act.get('activityType', '')
        fill, shape, _ = _node_styles(atype)
        caption = act.get('caption', '').replace('"', '\\"')
        label = nid if not caption else f'{nid}\\n({caption})'
        lines.append(
            f'  "{nid}" [label="{label}" shape={shape} style=filled fillcolor="{fill}"];'
        )

    lines.append('')

    seen = set()
    for edge in edges_list:
        src, dst = edge.get('from', ''), edge.get('to', '')
        etype = edge.get('type', 'NORMAL')
        if src not in activities or dst not in activities:
            continue
        key = (src, dst, etype)
        if key in seen:
            continue
        seen.add(key)
        eattr, _ = _EDGE_STYLES.get(etype, _EDGE_STYLES['NORMAL'])
        lines.append(f'  "{src}" -> "{dst}" [{eattr}];')

    lines.append('}')
    return '\n'.join(lines)


# ── draw.io export ────────────────────────────────────────────────────────────

def _xml_attr(value):
    """Escape a string for use inside an XML attribute value."""
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')


def export_drawio(filepath):
    """Return a draw.io XML string for the process. Raises on parse failure."""
    _, _, activities, edges_list = parse_lpd(filepath)
    coords = _get_coords(activities, edges_list)
    out_edges, in_edges = build_graph(activities, edges_list)
    topo = topological_sort(list(activities.keys()), out_edges, in_edges)

    cells = [
        '    <mxCell id="0"/>',
        '    <mxCell id="1" parent="0"/>',
    ]

    for nid in topo:
        act = activities[nid]
        atype = act.get('activityType', '')
        _, _, style = _node_styles(atype)
        caption = _xml_attr(act.get('caption', ''))
        nid_attr = _xml_attr(nid)
        cx, cy = coords[nid]
        x = cx - _NODE_W / 2
        y = cy - _NODE_H / 2
        h = 60 if atype == 'BRANCH' else _NODE_H   # diamonds look better taller
        label = nid_attr if not caption else f'{nid_attr} ({caption})'
        cells.append(
            f'    <mxCell id="{nid_attr}" value="{label}" style="{style}"'
            f' vertex="1" parent="1">'
        )
        cells.append(
            f'      <mxGeometry x="{x:.0f}" y="{y:.0f}"'
            f' width="{_NODE_W}" height="{h}" as="geometry"/>'
        )
        cells.append('    </mxCell>')

    seen = set()
    edge_id = 0
    for edge in edges_list:
        src, dst = edge.get('from', ''), edge.get('to', '')
        etype = edge.get('type', 'NORMAL')
        if src not in activities or dst not in activities:
            continue
        key = (src, dst, etype)
        if key in seen:
            continue
        seen.add(key)
        _, estyle = _EDGE_STYLES.get(etype, _EDGE_STYLES['NORMAL'])
        src_attr, dst_attr = _xml_attr(src), _xml_attr(dst)
        cells.append(
            f'    <mxCell id="e{edge_id}" style="{estyle}" edge="1"'
            f' source="{src_attr}" target="{dst_attr}" parent="1">'
        )
        cells.append('      <mxGeometry relative="1" as="geometry"/>')
        cells.append('    </mxCell>')
        edge_id += 1

    max_x = max((cx for cx, _ in coords.values()), default=800)
    max_y = max((cy for _, cy in coords.values()), default=600)
    page_w = int(max_x + _NODE_W + 80)
    page_h = int(max_y + _NODE_H + 80)

    body = '\n'.join(cells)
    return (
        f'<mxGraphModel dx="1422" dy="762" grid="1" gridSize="10" guides="1" '
        f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
        f'pageWidth="{page_w}" pageHeight="{page_h}" math="0" shadow="0">\n'
        f'  <root>\n'
        f'{body}\n'
        f'  </root>\n'
        f'</mxGraphModel>'
    )


# ── Graphviz render ───────────────────────────────────────────────────────────

def render_dot(dot_path, fmt):
    """Run `dot -T<fmt>` to produce a rendered image. Returns output path or raises."""
    if not shutil.which('dot'):
        raise RuntimeError(
            "'dot' not found on PATH.\n"
            "  Install Graphviz: https://graphviz.org/download/\n"
            "  Or paste the .dot file into https://dreampuf.github.io/GraphvizOnline"
        )
    out_path = os.path.splitext(dot_path)[0] + f'.{fmt}'
    result = subprocess.run(
        ['dot', f'-T{fmt}', dot_path, '-o', out_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot exited {result.returncode}: {result.stderr.strip()}")
    return out_path


# ── Per-file dispatch ─────────────────────────────────────────────────────────

_EXT = {'dot': '.dot', 'drawio': '.drawio'}
_FMT_FROM_EXT = {'.dot': 'dot', '.drawio': 'drawio'}


def _infer_format(path):
    return _FMT_FROM_EXT.get(os.path.splitext(path)[1].lower())


def process_file(filepath, out_path, fmt, render_fmt):
    """Export one file. Returns True on success, False on error."""
    print(f"Exporting: {filepath}")
    try:
        content = export_drawio(filepath) if fmt == 'drawio' else export_dot(filepath)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return False

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"  Written: {out_path}")

    if render_fmt:
        if fmt != 'dot':
            print("  NOTE: --render is only supported for DOT format, skipped.")
        else:
            try:
                rendered = render_dot(out_path, render_fmt)
                print(f"  Rendered: {rendered}")
            except RuntimeError as exc:
                print(f"  RENDER ERROR: {exc}")
                return False

    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='lpd_export',
        description='Export Infor IDP/IPA .lpd process files to diagram formats.',
        epilog="""
Formats:
  dot     Graphviz DOT.  No install needed — paste into:
          https://dreampuf.github.io/GraphvizOnline
          Or install Graphviz and use --render png/svg/pdf.
  drawio  draw.io XML.  Open in https://app.diagrams.net or the desktop app.
          From draw.io you can export to Visio .vsdx, PDF, PNG, SVG, and more.

Examples:
  python lpd_export.py MyProcess.lpd                       # write MyProcess.dot
  python lpd_export.py MyProcess.lpd --format drawio       # write MyProcess.drawio
  python lpd_export.py MyProcess.lpd -o diagram.drawio     # explicit output path
  python lpd_export.py MyProcess.lpd --render png          # .dot + render to .png
  python lpd_export.py --dir ./production/ --format drawio # batch export a folder

Tip: run lpd_layout.py first so node positions carry into the diagram.

Node colours: grey=START/END  blue=iterator  gold=BRANCH  red=EMAIL  green=WEBRN
Edge styles:  black=NORMAL  blue-dashed=BRANCH  red-dashed=ERROR
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('file', nargs='?', help='.lpd file to export')
    parser.add_argument('--dir', metavar='DIR',
                        help='Export all .lpd files in DIR (batch mode)')
    parser.add_argument('-o', '--output', metavar='FILE',
                        help='Output file path (single-file mode; extension overrides --format)')
    parser.add_argument('--format', metavar='FMT', choices=['dot', 'drawio'], default='dot',
                        help='Output format: dot (default) or drawio')
    parser.add_argument('--render', metavar='FMT', choices=['png', 'svg', 'pdf'],
                        help='After writing .dot, run Graphviz to render (dot format only)')

    args = parser.parse_args()

    if args.dir:
        if args.output:
            parser.error("--output cannot be used with --dir (paths are auto-derived)")
        if not os.path.isdir(args.dir):
            print(f"Directory not found: {args.dir}")
            sys.exit(1)
        targets = sorted(glob_module.glob(os.path.join(args.dir, '*.lpd')))
        if not targets:
            print(f"No .lpd files found in: {args.dir}")
            sys.exit(0)
    else:
        if not args.file:
            parser.error("Provide a file argument or use --dir")
        if not os.path.isfile(args.file):
            print(f"File not found: {args.file}")
            sys.exit(1)
        targets = [args.file]

    succeeded, failed = [], []
    for fp in targets:
        if args.output and len(targets) == 1:
            out_path = args.output
            fmt = _infer_format(out_path) or args.format
        else:
            fmt = args.format
            out_path = os.path.splitext(fp)[0] + _EXT[fmt]

        ok = process_file(fp, out_path, fmt, args.render)
        (succeeded if ok else failed).append(fp)
        if len(targets) > 1:
            print()

    if len(targets) > 1:
        print(f"Batch complete: {len(succeeded)} exported, {len(failed)} failed.")
        if failed:
            for fp in failed:
                print(f"  FAILED: {fp}")
        sys.exit(0 if not failed else 1)
    elif failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
