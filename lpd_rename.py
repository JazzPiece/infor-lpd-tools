#!/usr/bin/env python3
"""
lpd_rename.py -- Safely rename a node ID in an Infor IDP/IPA .lpd process file

Updates every reference to the old ID:
  - The activity's own id= attribute
  - All edge from= and to= attributes
  - All OnActivityError/activity goto targets

Creates a timestamped backup before writing. Use --preview to see what would
change without writing.

Usage:
  python lpd_rename.py <file.lpd> <old-id> <new-id>
  python lpd_rename.py <file.lpd> <old-id> <new-id> --preview

Requirements: Python 3.6+ (stdlib only). lpd_common.py must be in the same folder.
"""

import argparse
import os
import sys

from lpd_common import parse_lpd, write_lpd, make_backup, ITERATOR_TYPES


def find_references(root, old_id):
    """
    Return a list of (description, element, attr_name) for every reference
    to old_id in the document.
    """
    refs = []

    # Activity id= attribute
    for act in root.findall('.//activity'):
        if act.get('id') == old_id:
            refs.append((f"activity id='{old_id}'", act, 'id'))

    # Edge from= and to=
    for edge in root.findall('.//edge'):
        if edge.get('from') == old_id:
            refs.append((
                f"edge {edge.get('id')}: from='{old_id}' -> '{edge.get('to')}'",
                edge, 'from'
            ))
        if edge.get('to') == old_id:
            refs.append((
                f"edge {edge.get('id')}: '{edge.get('from')}' -> to='{old_id}'",
                edge, 'to'
            ))

    # OnActivityError/activity goto targets
    for act in root.findall('.//activity'):
        goto_el = act.find('.//OnActivityError/goto')
        act_el  = act.find('.//OnActivityError/activity')
        if (goto_el is not None and goto_el.text == 'true'
                and act_el is not None and act_el.text == old_id):
            refs.append((
                f"OnActivityError goto in '{act.get('id')}' targets '{old_id}'",
                act_el, None   # text node, not attribute
            ))

    return refs


def apply_rename(refs, old_id, new_id):
    """Apply new_id to all collected references. Modifies elements in-place."""
    for _desc, elem, attr in refs:
        if attr is None:
            elem.text = new_id
        else:
            elem.set(attr, new_id)


def main():
    parser = argparse.ArgumentParser(
        prog='lpd_rename',
        description='Rename a node ID and update all references in an .lpd file',
        epilog="""
Examples:
  python lpd_rename.py MyProcess.lpd GetEmployee1000 QueryActiveEmployees1000
  python lpd_rename.py MyProcess.lpd GetEmployee1000 QueryActiveEmployees1000 --preview
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('file',   help='.lpd file to modify')
    parser.add_argument('old_id', help='Current node ID to rename')
    parser.add_argument('new_id', help='New node ID to use')
    parser.add_argument('--preview', action='store_true',
                        help='Show what would change without writing the file')

    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"File not found: {args.file}")
        sys.exit(1)

    try:
        tree, root, activities, _ = parse_lpd(args.file)
    except Exception as e:
        print(f"Could not parse {args.file}: {e}")
        sys.exit(1)

    # Validate old_id exists
    if args.old_id not in activities:
        print(f"Node '{args.old_id}' not found in {os.path.basename(args.file)}")
        print(f"Available IDs: {sorted(activities.keys())}")
        sys.exit(1)

    # Validate new_id doesn't already exist
    if args.new_id in activities:
        print(f"Node '{args.new_id}' already exists -- choose a different new ID")
        sys.exit(1)

    refs = find_references(root, args.old_id)

    if not refs:
        print(f"No references to '{args.old_id}' found.")
        sys.exit(0)

    # Warn if renaming an iterator whose ItEnd also needs renaming
    old_act = activities[args.old_id]
    if old_act.get('activityType', '') in ITERATOR_TYPES:
        itend_id = f"End-{args.old_id}"
        new_itend_id = f"End-{args.new_id}"
        if itend_id in activities:
            print(f"NOTE: '{args.old_id}' is an iterator. Its paired ItEnd '{itend_id}'")
            print(f"      should also be renamed to '{new_itend_id}'.")
            print(f"      Run: python lpd_rename.py {args.file} {itend_id} {new_itend_id}")
            print()

    label = os.path.basename(args.file)
    print(f"{'Preview: ' if args.preview else ''}Renaming '{args.old_id}' -> '{args.new_id}' in {label}")
    print()
    for desc, _, _ in refs:
        print(f"  {desc}")
    print()
    print(f"  {len(refs)} reference{'s' if len(refs) != 1 else ''} to update.")

    if args.preview:
        print("\n(Preview only -- nothing written. Remove --preview to apply.)")
        sys.exit(0)

    backup = make_backup(args.file, label='rename')
    print(f"\nBackup:  {backup}")

    apply_rename(refs, args.old_id, args.new_id)
    write_lpd(tree, args.file)
    print(f"Done.    {args.file}")


if __name__ == '__main__':
    main()
