#!/usr/bin/env python3
"""
lpd_common.py — Shared utilities for the lpd-tools toolkit

Provides XML parsing, graph construction, topological sort, backup/restore,
and basic validation used by all lpd-tools scripts.

Import with:
    from lpd_common import parse_lpd, build_graph, topological_sort, ...

Requirements: Python 3.6+ (stdlib only)
"""

import glob
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime


# Node types that are iterators — must have a paired ItEnd node
ITERATOR_TYPES = {
    'ITERFR', 'QUERY', 'LM', 'LOOP', 'DATAEX',
    'LDAPQ', 'RMQR', 'IONIN', 'SQLQR', 'FORMTXN',
}

# Node types blocked in multi-tenant hosted Infor
BLOCKED_TYPES = {'CUSAD', 'SQLUP', 'SQLQR'}

# Email prop names on EMAIL nodes that should not contain raw addresses
EMAIL_ADDR_PROPS = {'to', 'from', 'cc', 'bcc'}

# Regex: a string that looks like an email address
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_lpd(filepath):
    """
    Parse an .lpd file. Returns (tree, root, activities_dict, edges_list).

    activities_dict: {id: element} for every <activity> with an id attribute
    edges_list:      list of dicts with keys id, from, to, type
    """
    ET.register_namespace('', '')
    tree = ET.parse(filepath)
    root = tree.getroot()

    activities = {}
    for act in root.findall('.//activity'):
        aid = act.get('id')
        if aid:
            activities[aid] = act

    edges = []
    for edge in root.findall('.//edge'):
        edges.append({
            'id':   edge.get('id'),
            'from': edge.get('from'),
            'to':   edge.get('to'),
            'type': edge.get('edgeType', 'NORMAL'),
        })

    return tree, root, activities, edges


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph(activities, edges):
    """
    Return (out_edges, in_edges).
    Both are dicts: node_id → list of (neighbor_id, edge_type).
    Only includes edges where both endpoints exist in activities.
    """
    out_edges = defaultdict(list)
    in_edges  = defaultdict(list)
    all_ids   = set(activities.keys())

    for e in edges:
        src, dst = e['from'], e['to']
        if src in all_ids and dst in all_ids:
            out_edges[src].append((dst, e['type']))
            in_edges[dst].append((src, e['type']))

    return dict(out_edges), dict(in_edges)


# ── Topological sort (Kahn's algorithm) ──────────────────────────────────────

def topological_sort(node_ids, out_edges, in_edges):
    """
    Return nodes in topological order using Kahn's algorithm.
    Handles disconnected graphs. Any cycle nodes are appended last (sorted).
    """
    in_degree = {n: 0 for n in node_ids}
    for n in node_ids:
        for (dst, _) in out_edges.get(n, []):
            in_degree[dst] = in_degree.get(dst, 0) + 1

    queue = deque(n for n in node_ids if in_degree[n] == 0)
    order = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for (dst, _) in out_edges.get(n, []):
            in_degree[dst] -= 1
            if in_degree[dst] == 0:
                queue.append(dst)

    remaining = set(node_ids) - set(order)
    order.extend(sorted(remaining))
    return order


# ── Validation ────────────────────────────────────────────────────────────────

def validate_refs(root, activity_ids):
    """
    Check edge from/to and OnActivityError goto references.
    Returns list of error strings (empty = clean).
    """
    errors = []
    for edge in root.findall('.//edge'):
        f, t = edge.get('from'), edge.get('to')
        if f not in activity_ids:
            errors.append(f"Edge {edge.get('id')}: from='{f}' references missing node")
        if t not in activity_ids:
            errors.append(f"Edge {edge.get('id')}: to='{t}' references missing node")

    for act in root.findall('.//activity'):
        goto_el = act.find('.//OnActivityError/goto')
        act_el  = act.find('.//OnActivityError/activity')
        if goto_el is not None and goto_el.text == 'true':
            if act_el is not None and act_el.text and act_el.text not in activity_ids:
                errors.append(
                    f"{act.get('id')}: OnActivityError goto='{act_el.text}' references missing node"
                )
    return errors


# ── Prop helpers ──────────────────────────────────────────────────────────────

def get_prop(activity, prop_name):
    """Return the anyData text for a named prop on an activity, or '' if absent/empty."""
    for prop in activity.findall('prop'):
        if prop.get('name') == prop_name:
            any_data = prop.find('anyData')
            return (any_data.text or '').strip() if any_data is not None else ''
    return ''


def iter_props(activity):
    """Yield (name, value) for every simple prop on an activity."""
    for prop in activity.findall('prop'):
        if prop.get('propType') == 'SIMPLE':
            name = prop.get('name', '')
            any_data = prop.find('anyData')
            value = (any_data.text or '').strip() if any_data is not None else ''
            yield name, value


def all_cdata_text(activity):
    """Return all CDATA/text content from an activity as a single string."""
    parts = []
    for elem in activity.iter():
        if elem.text:
            parts.append(elem.text.strip())
    return ' '.join(p for p in parts if p)


# ── Backup / Restore ──────────────────────────────────────────────────────────

def make_backup(filepath, label='layout'):
    """
    Copy file to <base>_<label>_backup_YYYYMMDD_HHMMSS<ext>.
    Returns the backup path.
    """
    base, ext = os.path.splitext(filepath)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f"{base}_{label}_backup_{timestamp}{ext}"
    shutil.copy2(filepath, backup_path)
    return backup_path


def find_latest_backup(filepath, label='layout'):
    """Return path to the most recent backup for this file+label, or None."""
    base, ext = os.path.splitext(filepath)
    pattern = f"{base}_{label}_backup_*{ext}"
    candidates = sorted(glob.glob(pattern))
    return candidates[-1] if candidates else None


def restore_backup(filepath, label='layout'):
    """Copy most recent backup back over the original. Returns True on success."""
    backup = find_latest_backup(filepath, label)
    if not backup:
        print(f"No {label} backup found for {filepath}")
        return False
    shutil.copy2(backup, filepath)
    print(f"Restored from: {backup}")
    return True


# ── XML write helper ──────────────────────────────────────────────────────────

def write_lpd(tree, filepath):
    """
    Write XML tree back as single-line with proper declaration.
    Normalises single-quoted declaration to double-quoted (IDP convention).
    """
    tree.write(filepath, encoding='UTF-8', xml_declaration=True)
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace(
        "<?xml version='1.0' encoding='UTF-8'?>",
        '<?xml version="1.0" encoding="UTF-8"?>', 1
    )
    with open(filepath, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
