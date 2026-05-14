#!/usr/bin/env python3
"""
lpd_validate.py  -  Structural validator for Infor IDP/IPA .lpd process files

Catches errors that IDP only surfaces at load time (or silently ignores).
Run before every deploy to catch missing ItEnd pairs, broken edges, blocked
nodes, hardcoded email addresses, and other common mistakes.

Usage:
  python lpd_validate.py <file.lpd>              # validate one file
  python lpd_validate.py file1.lpd file2.lpd     # validate multiple files
  python lpd_validate.py --dir ./production/     # validate all .lpd in a folder
  python lpd_validate.py <file.lpd> --strict     # treat WARNs as errors (exit 1)

Exit codes:
  0  -  no errors (warnings allowed unless --strict)
  1  -  one or more errors found (or warnings with --strict)

Requirements: Python 3.6+ (stdlib only). lpd_common.py must be in the same folder.
"""

import argparse
import glob
import os
import re
import sys

from lpd_common import (
    parse_lpd, build_graph, topological_sort,
    validate_refs, get_prop, iter_props,
    ITERATOR_TYPES, BLOCKED_TYPES, EMAIL_ADDR_PROPS,
)

# -- Check definitions ---------------------------------------------------------

ITEND_CLASS = {
    'FORMTXN': 'FgaFormTxnIterEnd',
}
DEFAULT_ITEND_CLASS = 'FgaIterEnd'

# Required props that must be non-empty per node type
REQUIRED_PROPS = {
    'EMAIL':  [('to', 'recipient address'), ('content', 'message body')],
    'WEBRN':  [('callString', 'endpoint path')],
    'LSNAD':  [('lawsonApi', 'Lawson API string')],
    'ITERFR': [('filePathName', 'file path')],
}

# Regex: a string that looks like an email address
_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')


def _is_hardcoded_email(value):
    """True if value contains a raw email address (not a _configuration token)."""
    if '<!_configuration' in value:
        return False
    return bool(_EMAIL_RE.search(value))


# -- Per-file validation -------------------------------------------------------

def validate_file(filepath):
    """
    Run all checks on one .lpd file.
    Returns (errors, warnings)  -  each a list of strings.
    """
    errors   = []
    warnings = []

    try:
        tree, root, activities, edges = parse_lpd(filepath)
    except Exception as e:
        return [f"Could not parse file: {e}"], []

    activity_ids = set(activities.keys())
    out_edges, in_edges = build_graph(activities, edges)
    topo = topological_sort(list(activity_ids), out_edges, in_edges)

    # -- 1. Duplicate activity IDs ---------------------------------------------
    raw_ids = [a.get('id') for a in root.findall('.//activity') if a.get('id')]
    seen, dupes = set(), set()
    for aid in raw_ids:
        if aid in seen:
            dupes.add(aid)
        seen.add(aid)
    for aid in sorted(dupes):
        errors.append(f"Duplicate activity id: '{aid}'")

    # -- 2. Edge reference integrity (from lpd_common) -------------------------
    for msg in validate_refs(root, activity_ids):
        errors.append(msg)

    # -- 3. START / END presence -----------------------------------------------
    starts = [a for a in activities.values() if a.get('activityType') == 'START']
    ends   = [a for a in activities.values() if a.get('activityType') == 'END']

    if len(starts) == 0:
        errors.append("No START node found")
    elif len(starts) > 1:
        errors.append(f"Multiple START nodes: {[a.get('id') for a in starts]}")

    if len(ends) == 0:
        errors.append("No END node found")

    # -- 4. Error-path END missing processStatus=ERROR -------------------------
    # Find END nodes reachable only via ERROR edges from main flow
    error_only_nodes = set()
    for nid, act in activities.items():
        preds = in_edges.get(nid, [])
        if preds and all(t == 'ERROR' for _, t in preds):
            error_only_nodes.add(nid)

    def _all_predecessors_error_only(nid, visited=None):
        if visited is None:
            visited = set()
        if nid in visited:
            return True
        visited.add(nid)
        preds = in_edges.get(nid, [])
        if not preds:
            return False
        return all(
            t == 'ERROR' or _all_predecessors_error_only(p, visited)
            for p, t in preds
        )

    for act in ends:
        nid = act.get('id')
        status = get_prop(act, 'processStatus')
        if _all_predecessors_error_only(nid):
            if status != 'ERROR':
                warnings.append(
                    f"{nid} (END): reachable only via error path  -  "
                    f"set processStatus=ERROR (currently '{status or 'empty'}')"
                )

    # -- 5. Blocked node types -------------------------------------------------
    for nid, act in activities.items():
        atype = act.get('activityType', '')
        if atype in BLOCKED_TYPES:
            errors.append(
                f"{nid} ({atype}): blocked node type  -  "
                f"not allowed in multi-tenant hosted Infor"
            )

    # -- 6. Missing ItEnd pairs ------------------------------------------------
    for nid, act in activities.items():
        atype = act.get('activityType', '')
        if atype in ITERATOR_TYPES:
            expected_end_id = f"End-{nid}"
            if expected_end_id not in activity_ids:
                errors.append(
                    f"{nid} ({atype}): missing paired ItEnd node "
                    f"(expected id='{expected_end_id}')"
                )

    # -- 7. BRANCH edges correctness -------------------------------------------
    # HRUA and UA (UserAction) nodes legitimately use BRANCH edges for
    # approval routing — only flag true BRANCH activityType node mismatches.
    branch_node_ids = {
        nid for nid, act in activities.items()
        if act.get('activityType') == 'BRANCH'
    }
    branch_edge_ok_types = {'BRANCH', 'HRUA', 'UA'}
    for edge in root.findall('.//edge'):
        src       = edge.get('from', '')
        edge_type = edge.get('edgeType', 'NORMAL')
        src_type  = activities[src].get('activityType', '') if src in activities else ''
        if src in branch_node_ids and edge_type != 'BRANCH':
            errors.append(
                f"Edge {edge.get('id')}: BRANCH node '{src}' has "
                f"non-BRANCH outgoing edge (edgeType='{edge_type}')"
            )
        if src not in branch_node_ids and edge_type == 'BRANCH' and src_type not in branch_edge_ok_types:
            errors.append(
                f"Edge {edge.get('id')}: node '{src}' ({src_type}) "
                f"uses edgeType='BRANCH' unexpectedly"
            )

    # -- 8. Hardcoded email addresses ------------------------------------------
    for nid, act in activities.items():
        atype = act.get('activityType', '')
        if atype == 'EMAIL':
            for prop_name in EMAIL_ADDR_PROPS:
                value = get_prop(act, prop_name)
                if value and _is_hardcoded_email(value):
                    warnings.append(
                        f"{nid} (EMAIL): hardcoded address in '{prop_name}': "
                        f'"{value}"  -  use <!_configuration.*> token instead'
                    )

    # -- 9. Empty required fields ----------------------------------------------
    for atype, required in REQUIRED_PROPS.items():
        for nid, act in activities.items():
            if act.get('activityType') == atype:
                for prop_name, label in required:
                    value = get_prop(act, prop_name)
                    if not value:
                        warnings.append(
                            f"{nid} ({atype}): required field '{prop_name}' "
                            f"({label}) is empty  -  configure in IDP"
                        )

    return errors, warnings


# -- Output formatting ---------------------------------------------------------

def report(filepath, errors, warnings, show_ok=True):
    """Print validation results for one file. Returns True if any issues found."""
    node_count = 0
    try:
        _, root, _, _ = parse_lpd(filepath)
        node_count = len(root.findall('.//activity'))
    except Exception:
        pass

    label = os.path.basename(filepath)
    has_issues = bool(errors or warnings)

    if not has_issues:
        if show_ok:
            print(f"  OK     {label}  ({node_count} nodes)")
        return False

    print(f"\nValidating: {label}  ({node_count} nodes)")
    for msg in errors:
        print(f"  ERROR  {msg}")
    for msg in warnings:
        print(f"  WARN   {msg}")

    summary_parts = []
    if errors:
        summary_parts.append(f"{len(errors)} error{'s' if len(errors) != 1 else ''}")
    if warnings:
        summary_parts.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    print(f"\n  {', '.join(summary_parts)}.")
    return True


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='lpd_validate',
        description='Validate Infor IDP/IPA .lpd process files',
        epilog="""
Examples:
  python lpd_validate.py MyProcess.lpd
  python lpd_validate.py --dir ./production/
  python lpd_validate.py *.lpd --strict
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('files', nargs='*', help='.lpd file(s) to validate')
    parser.add_argument('--dir', metavar='DIR',
                        help='Validate all .lpd files in this directory')
    parser.add_argument('--strict', action='store_true',
                        help='Treat warnings as errors (exit 1 if any warnings)')

    args = parser.parse_args()

    # Collect files
    targets = list(args.files)
    if args.dir:
        targets += glob.glob(os.path.join(args.dir, '*.lpd'))

    if not targets:
        parser.print_help()
        sys.exit(0)

    # Deduplicate and sort
    targets = sorted(set(targets))

    total_errors   = 0
    total_warnings = 0
    files_with_issues = 0

    if len(targets) > 1:
        print(f"Validating {len(targets)} files...\n")

    for filepath in targets:
        if not os.path.isfile(filepath):
            print(f"  NOT FOUND  {filepath}")
            total_errors += 1
            continue

        errors, warnings = validate_file(filepath)
        total_errors   += len(errors)
        total_warnings += len(warnings)

        had_issues = report(filepath, errors, warnings, show_ok=True)
        if had_issues:
            files_with_issues += 1

    if len(targets) > 1:
        print(f"\n{'-'*50}")
        print(f"Files checked: {len(targets)}")
        if files_with_issues:
            print(f"Files with issues: {files_with_issues}")
        print(f"Total errors: {total_errors}  |  Total warnings: {total_warnings}")

    exit_code = 1 if total_errors > 0 else 0
    if args.strict and total_warnings > 0:
        exit_code = 1

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
