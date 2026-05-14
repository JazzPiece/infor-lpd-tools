#!/usr/bin/env python3
"""
lpd_search.py -- Search across multiple Infor IDP/IPA .lpd process files

Find which processes use specific node types, prop values, text patterns,
hardcoded emails, or are missing error handlers. Useful for impact analysis
before changes and org-wide audits.

Usage:
  python lpd_search.py <dir> --node WEBRN
  python lpd_search.py <dir> --prop to "reports@"
  python lpd_search.py <dir> --text "VendorImport"
  python lpd_search.py <dir> --hardcoded-email
  python lpd_search.py <dir> --no-error-handler
  python lpd_search.py <dir> --node LM --prop transactionString "Employee"

  <dir> can also be a single .lpd file or a glob pattern.

Multiple search flags can be combined (AND logic -- file must match all).

Requirements: Python 3.6+ (stdlib only). lpd_common.py must be in the same folder.
"""

import argparse
import glob
import os
import sys

from lpd_common import parse_lpd, get_prop, all_cdata_text, EMAIL_ADDR_PROPS, EMAIL_RE


# ── Search functions — each returns list of match description strings ─────────

def search_node_type(activities, node_type):
    """Find nodes with a specific activityType."""
    matches = []
    for nid, act in activities.items():
        if act.get('activityType', '').upper() == node_type.upper():
            caption = act.get('caption', '')
            matches.append(f"{nid} ({act.get('activityType')})"
                           + (f" -- caption: '{caption}'" if caption else ''))
    return matches


def search_prop(activities, prop_name, value_substr):
    """Find nodes where a prop named prop_name contains value_substr."""
    matches = []
    for nid, act in activities.items():
        prop_val = get_prop(act, prop_name)
        if prop_val and value_substr.lower() in prop_val.lower():
            display = (prop_val[:60] + '...') if len(prop_val) > 60 else prop_val
            matches.append(f"{nid} ({act.get('activityType')}): {prop_name}='{display}'")
    return matches


def search_text(activities, text_substr):
    """Full text search across all CDATA content in all activities."""
    matches = []
    for nid, act in activities.items():
        full_text = all_cdata_text(act)
        if text_substr.lower() in full_text.lower():
            matches.append(f"{nid} ({act.get('activityType')})")
    return matches


def search_hardcoded_email(activities):
    """Find EMAIL nodes with raw email addresses (not _configuration tokens)."""
    matches = []
    for nid, act in activities.items():
        if act.get('activityType') != 'EMAIL':
            continue
        for prop_name in EMAIL_ADDR_PROPS:
            value = get_prop(act, prop_name)
            if value and '<!_configuration' not in value and EMAIL_RE.search(value):
                matches.append(
                    f"{nid} (EMAIL): '{prop_name}' has hardcoded address: '{value}'"
                )
    return matches


def search_no_error_handler(activities, edges):
    """Find processes with no dedicated error handling node."""
    # Look for an EMAIL or END node whose id or caption suggests error handling
    error_handler_ids = {
        nid for nid, act in activities.items()
        if ('error' in (act.get('id') or '').lower() or
            'error' in (act.get('caption') or '').lower())
    }
    # Also check: any node receives an ERROR edge
    error_targets = {e['to'] for e in edges if e['type'] == 'ERROR'}

    if not error_handler_ids and not error_targets:
        return ['(no error handler found -- no node with "error" in id/caption, no ERROR edges)']
    return []


# ── Per-file search ───────────────────────────────────────────────────────────

def search_file(filepath, args):
    """
    Run enabled searches on one file.
    Returns list of match groups: [(search_label, [match_strings]), ...]
    """
    try:
        _, _, activities, edges = parse_lpd(filepath)
    except Exception as e:
        return [(f'parse error', [str(e)])]

    results = []

    if args.node:
        matches = search_node_type(activities, args.node)
        if matches:
            results.append((f'--node {args.node}', matches))

    if args.prop:
        prop_name, value_substr = args.prop
        matches = search_prop(activities, prop_name, value_substr)
        if matches:
            results.append((f'--prop {prop_name} "{value_substr}"', matches))

    if args.text:
        matches = search_text(activities, args.text)
        if matches:
            results.append((f'--text "{args.text}"', matches))

    if args.hardcoded_email:
        matches = search_hardcoded_email(activities)
        if matches:
            results.append(('--hardcoded-email', matches))

    if args.no_error_handler:
        matches = search_no_error_handler(activities, edges)
        if matches:
            results.append(('--no-error-handler', matches))

    # AND logic: only return if ALL enabled searches matched
    enabled_count = sum([
        bool(args.node),
        bool(args.prop),
        bool(args.text),
        bool(args.hardcoded_email),
        bool(args.no_error_handler),
    ])
    matched_count = len(results)

    if matched_count < enabled_count:
        return []   # didn't match all criteria

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog='lpd_search',
        description='Search across Infor IDP/IPA .lpd process files',
        epilog="""
Examples:
  python lpd_search.py ./production/ --node WEBRN
  python lpd_search.py ./production/ --prop to "reports@"
  python lpd_search.py ./production/ --text "VendorImport"
  python lpd_search.py ./production/ --hardcoded-email
  python lpd_search.py ./production/ --no-error-handler
  python lpd_search.py ./production/ --node LM --prop transactionString "Employee"
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('path', help='Directory, .lpd file, or glob pattern to search')
    parser.add_argument('--node',  metavar='TYPE',
                        help='Find files containing a node of this activityType')
    parser.add_argument('--prop',  nargs=2, metavar=('NAME', 'VALUE'),
                        help='Find files where prop NAME contains VALUE substring')
    parser.add_argument('--text',  metavar='STRING',
                        help='Full text search across all CDATA content')
    parser.add_argument('--hardcoded-email', action='store_true',
                        help='Find EMAIL nodes with hardcoded addresses (not _configuration tokens)')
    parser.add_argument('--no-error-handler', action='store_true',
                        help='Find processes missing a dedicated error handler')
    parser.add_argument('--files-only', action='store_true',
                        help='Show only file names, no match details')

    args = parser.parse_args()

    # Require at least one search flag
    if not any([args.node, args.prop, args.text, args.hardcoded_email, args.no_error_handler]):
        parser.error("Specify at least one search flag (--node, --prop, --text, --hardcoded-email, --no-error-handler)")

    # Collect files
    if os.path.isdir(args.path):
        targets = sorted(glob.glob(os.path.join(args.path, '*.lpd')))
    elif os.path.isfile(args.path):
        targets = [args.path]
    else:
        targets = sorted(glob.glob(args.path))

    if not targets:
        print(f"No .lpd files found at: {args.path}")
        sys.exit(0)

    # Build search description for header
    search_parts = []
    if args.node:          search_parts.append(f"--node {args.node}")
    if args.prop:          search_parts.append(f"--prop {args.prop[0]} \"{args.prop[1]}\"")
    if args.text:          search_parts.append(f"--text \"{args.text}\"")
    if args.hardcoded_email: search_parts.append("--hardcoded-email")
    if args.no_error_handler: search_parts.append("--no-error-handler")
    print(f"Searching {len(targets)} file(s) for {' AND '.join(search_parts)} ...\n")

    total_matches = 0
    files_matched = 0

    for filepath in targets:
        file_results = search_file(filepath, args)
        if not file_results:
            continue

        files_matched += 1
        label = os.path.basename(filepath)
        match_count = sum(len(m) for _, m in file_results)
        total_matches += match_count

        if args.files_only:
            print(f"  {label}")
        else:
            print(f"  {label}  ({match_count} match{'es' if match_count != 1 else ''})")
            for _search_label, matches in file_results:
                for match in matches:
                    print(f"    - {match}")
            print()

    if files_matched == 0:
        print("  No matches found.")
    else:
        print(f"{total_matches} match{'es' if total_matches != 1 else ''} across {files_matched} file{'s' if files_matched != 1 else ''}.")


if __name__ == '__main__':
    main()
