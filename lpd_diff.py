#!/usr/bin/env python3
"""
lpd_diff.py -- Compare two versions of an Infor IDP/IPA .lpd process file

Shows what changed between an original and a modified version:
  - Nodes added / removed / changed (prop-level diff)
  - Edges added / removed

Useful on change-window days to verify exactly what was modified before deploy.

Usage:
  python lpd_diff.py <original.lpd> <new.lpd>
  python lpd_diff.py <original.lpd> <new.lpd> --brief    # summary counts only
  python lpd_diff.py <original.lpd> <new.lpd> --no-coords # skip x/y changes

Requirements: Python 3.6+ (stdlib only). lpd_common.py must be in the same folder.
"""

import argparse
import os
import sys

from lpd_common import parse_lpd, iter_props


# ── Helpers ───────────────────────────────────────────────────────────────────

def activity_on_error_summary(act):
    """
    Return a compact string summarising OnActivityError settings.
    Included as a synthetic prop so error-handler changes surface in diffs.
    """
    oae = act.find('OnActivityError')
    if oae is None:
        return ''
    goto_el   = oae.find('goto')
    act_el    = oae.find('activity')
    log_el    = oae.find('log')
    goto      = (goto_el.text  or 'false') if goto_el  is not None else 'false'
    target    = (act_el.text   or '')      if act_el   is not None else ''
    log       = (log_el.text   or 'false') if log_el   is not None else 'false'
    parts = [f'goto={goto}']
    if goto == 'true' and target:
        parts.append(f'activity={target}')
    parts.append(f'log={log}')
    return ','.join(parts)


def activity_props(act):
    """Return dict of {prop_name: value} for an activity (excludes x, y, id, caption)."""
    props = {}
    for name, value in iter_props(act):
        if name not in ('_activityCheckPoint',):
            props[name] = value
    # Synthetic prop so OnActivityError changes surface in the diff
    oae = activity_on_error_summary(act)
    if oae:
        props['<OnActivityError>'] = oae
    return props


def activity_summary(act):
    """Short label: 'NodeId (TYPE)'"""
    return f"{act.get('id')} ({act.get('activityType', '?')})"


def edge_key(edge_dict):
    """Hashable edge identity: (from, to, edgeType)."""
    return (edge_dict['from'], edge_dict['to'], edge_dict['type'])


def edge_label(edge_dict):
    return f"{edge_dict['type']:6}  {edge_dict['from']} -> {edge_dict['to']}"


# ── Diff logic ────────────────────────────────────────────────────────────────

def diff_files(orig_path, new_path, skip_coords=False):
    """
    Return a dict with keys:
      nodes_added, nodes_removed, nodes_changed,
      edges_added, edges_removed
    Each value is a list of human-readable strings.
    """
    _, _, orig_acts, orig_edges = parse_lpd(orig_path)
    _, _, new_acts,  new_edges  = parse_lpd(new_path)

    orig_ids = set(orig_acts.keys())
    new_ids  = set(new_acts.keys())

    result = {
        'nodes_added':   [],
        'nodes_removed': [],
        'nodes_changed': [],
        'edges_added':   [],
        'edges_removed': [],
    }

    # Nodes added / removed
    for nid in sorted(new_ids - orig_ids):
        result['nodes_added'].append(f"+ {activity_summary(new_acts[nid])}")

    for nid in sorted(orig_ids - new_ids):
        result['nodes_removed'].append(f"- {activity_summary(orig_acts[nid])}")

    # Nodes changed (same ID, different props or attributes)
    for nid in sorted(orig_ids & new_ids):
        orig_act = orig_acts[nid]
        new_act  = new_acts[nid]
        changes  = []

        # Check activityType and caption changes
        for attr in ('activityType', 'caption'):
            ov = orig_act.get(attr, '')
            nv = new_act.get(attr, '')
            if ov != nv:
                changes.append(f"    {attr}: '{ov}' -> '{nv}'")

        # Check coordinate changes (optional)
        if not skip_coords:
            for attr in ('x', 'y'):
                ov = orig_act.get(attr, '')
                nv = new_act.get(attr, '')
                if ov != nv:
                    changes.append(f"    {attr}: {ov} -> {nv}")

        # Check prop changes
        orig_props = activity_props(orig_act)
        new_props  = activity_props(new_act)
        all_prop_names = sorted(set(orig_props) | set(new_props))
        for pname in all_prop_names:
            ov = orig_props.get(pname, '<absent>')
            nv = new_props.get(pname, '<absent>')
            if ov != nv:
                # Truncate long values for readability
                ov_display = (ov[:80] + '...') if len(ov) > 80 else ov
                nv_display = (nv[:80] + '...') if len(nv) > 80 else nv
                changes.append(f"    {pname}: '{ov_display}' -> '{nv_display}'")

        if changes:
            result['nodes_changed'].append(
                f"~ {activity_summary(orig_act)}"
            )
            result['nodes_changed'].extend(changes)

    # Edges added / removed
    orig_edge_keys = {edge_key(e): e for e in orig_edges}
    new_edge_keys  = {edge_key(e): e for e in new_edges}

    for k in sorted(new_edge_keys.keys() - orig_edge_keys.keys()):
        result['edges_added'].append(f"+ {edge_label(new_edge_keys[k])}")

    for k in sorted(orig_edge_keys.keys() - new_edge_keys.keys()):
        result['edges_removed'].append(f"- {edge_label(orig_edge_keys[k])}")

    return result


# ── Output ────────────────────────────────────────────────────────────────────

def print_section(title, lines):
    if not lines:
        return
    print(f"\n{title} ({len([l for l in lines if l.startswith(('+', '-', '~'))])}):")
    for line in lines:
        print(f"  {line}")


def _summary_parts(result):
    """Build the human-readable change-count list shared by brief and full output."""
    na = len(result['nodes_added'])
    nr = len(result['nodes_removed'])
    nc = len([l for l in result['nodes_changed'] if l.startswith('~')])
    ea = len(result['edges_added'])
    er = len(result['edges_removed'])
    parts = []
    if na: parts.append(f"{na} node(s) added")
    if nr: parts.append(f"{nr} node(s) removed")
    if nc: parts.append(f"{nc} node(s) changed")
    if ea: parts.append(f"{ea} edge(s) added")
    if er: parts.append(f"{er} edge(s) removed")
    return parts


def print_diff(orig_path, new_path, result, brief=False):
    orig_label = os.path.basename(orig_path)
    new_label  = os.path.basename(new_path)
    print(f"Diff: {orig_label}  ->  {new_label}")

    parts = _summary_parts(result)

    if not parts:
        print("\n  No differences found.")
        return

    if brief:
        print("  " + ",  ".join(parts))
        return

    print_section("ADDED nodes", result['nodes_added'])
    print_section("REMOVED nodes", result['nodes_removed'])

    if result['nodes_changed']:
        changed_node_count = len([l for l in result['nodes_changed'] if l.startswith('~')])
        print(f"\nCHANGED nodes ({changed_node_count}):")
        for line in result['nodes_changed']:
            print(f"  {line}")

    print_section("ADDED edges", result['edges_added'])
    print_section("REMOVED edges", result['edges_removed'])

    print(f"\n{',  '.join(parts)}.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='lpd_diff',
        description='Compare two versions of an .lpd process file',
        epilog="""
Examples:
  python lpd_diff.py OriginalProcess.lpd NewProcess.lpd
  python lpd_diff.py v1.lpd v2.lpd --brief
  python lpd_diff.py v1.lpd v2.lpd --no-coords
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('original', help='Original (before) .lpd file')
    parser.add_argument('new',      help='New (after) .lpd file')
    parser.add_argument('--brief', action='store_true',
                        help='Show summary counts only, no detail')
    parser.add_argument('--no-coords', action='store_true',
                        help='Skip x/y coordinate differences (layout-only changes)')

    args = parser.parse_args()

    for path in (args.original, args.new):
        if not os.path.isfile(path):
            print(f"File not found: {path}")
            sys.exit(1)

    try:
        result = diff_files(args.original, args.new, skip_coords=args.no_coords)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    print_diff(args.original, args.new, result, brief=args.brief)


if __name__ == '__main__':
    main()
