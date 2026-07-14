#!/usr/bin/env python3
"""
filter_export.py — Filter a claude.ai data export down to a single account owner.

A claude.ai "Export data" bundle contains:
  - conversations.json : [ { uuid, name, account:{uuid}, chat_messages:[...] }, ... ]
  - users.json         : [ { uuid, full_name, email_address }, ... ]   (the account owner)
  - projects.json      : (ignored here)

This script keeps ONLY the conversations belonging to the named owner and DROPS
(deletes) every other conversation. It writes a filtered file and prints a JSON
summary to stdout so the calling skill can report counts and drive the ingest.

Usage:
  filter_export.py --input <conversations.json> --owner "Vlad Munteanu" \
      [--users <users.json>] [--output <path>] [--dry-run]

Exit codes:
  0  success (filtered file written, or dry-run)
  2  input not found / unreadable
  3  owner could not be resolved AND no conversation matched (nothing deleted, nothing kept)
"""
import argparse
import json
import os
import sys


def eprint(*a):
    print(*a, file=sys.stderr)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_owner_uuids(users, owner_name):
    """Return the set of account uuids whose full_name matches owner_name (case-insensitive)."""
    want = owner_name.strip().casefold()
    uuids = set()
    matched_names = []
    for u in users or []:
        name = (u.get("full_name") or u.get("name") or "").strip()
        if name and name.casefold() == want:
            uid = u.get("uuid") or u.get("account_uuid")
            if uid:
                uuids.add(uid)
                matched_names.append(name)
    return uuids, matched_names


def conv_account_uuid(conv):
    acct = conv.get("account")
    if isinstance(acct, dict):
        return acct.get("uuid")
    if isinstance(acct, str):
        return acct
    return None


def conv_owner_name(conv):
    """Best-effort owner name embedded on a conversation (fallback path)."""
    acct = conv.get("account")
    if isinstance(acct, dict):
        return (acct.get("full_name") or acct.get("name") or "").strip()
    return (conv.get("full_name") or "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="path to conversations.json")
    ap.add_argument("--owner", required=True, help='account owner full name, e.g. "Vlad Munteanu"')
    ap.add_argument("--users", default=None, help="path to users.json (defaults to sibling of --input)")
    ap.add_argument("--output", default=None, help="filtered output path (default: <input>.<owner-slug>.json)")
    ap.add_argument("--in-place", action="store_true", help="overwrite --input, deleting non-owner chats from it directly")
    ap.add_argument("--dry-run", action="store_true", help="report counts but do not write")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        eprint(f"[filter] input not found: {args.input}")
        sys.exit(2)

    try:
        convs = load_json(args.input)
    except Exception as e:
        eprint(f"[filter] failed to parse {args.input}: {e}")
        sys.exit(2)

    if not isinstance(convs, list):
        eprint("[filter] expected conversations.json to be a JSON array")
        sys.exit(2)

    # Resolve owner account uuid(s) from users.json when available.
    users_path = args.users
    if users_path is None:
        cand = os.path.join(os.path.dirname(os.path.abspath(args.input)), "users.json")
        users_path = cand if os.path.isfile(cand) else None

    owner_uuids, matched_names = (set(), [])
    if users_path and os.path.isfile(users_path):
        try:
            owner_uuids, matched_names = resolve_owner_uuids(load_json(users_path), args.owner)
        except Exception as e:
            eprint(f"[filter] warning: could not read {users_path}: {e}")

    kept, deleted = [], []
    method = None

    if owner_uuids:
        # Primary path: match by account uuid resolved from users.json.
        method = "account_uuid"
        for c in convs:
            (kept if conv_account_uuid(c) in owner_uuids else deleted).append(c)
    else:
        # Fallback path: match an owner name embedded on the conversation itself.
        method = "embedded_name"
        want = args.owner.strip().casefold()
        any_name_seen = False
        for c in convs:
            nm = conv_owner_name(c)
            if nm:
                any_name_seen = True
            (kept if nm.casefold() == want else deleted).append(c)
        if not any_name_seen:
            # No per-conversation name and no users.json match: we cannot safely
            # distinguish owners. Do NOT silently delete everything — bail loudly.
            eprint(
                "[filter] could not resolve owner: no users.json match and no owner "
                "name on any conversation. Refusing to guess. Inspect the export schema "
                "and pass --users or verify --owner."
            )
            summary = {
                "status": "unresolved",
                "owner": args.owner,
                "total": len(convs),
                "kept": 0,
                "deleted": 0,
                "method": method,
            }
            print(json.dumps(summary))
            sys.exit(3)

    if args.in_place:
        out_path = args.input
    elif args.output is not None:
        out_path = args.output
    else:
        slug = args.owner.strip().lower().replace(" ", "-")
        base, ext = os.path.splitext(args.input)
        out_path = f"{base}.{slug}{ext or '.json'}"

    if not args.dry_run:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False, indent=2)

    summary = {
        "status": "ok",
        "owner": args.owner,
        "owner_uuids": sorted(owner_uuids),
        "matched_owner_names": matched_names,
        "method": method,
        "total": len(convs),
        "kept": len(kept),
        "deleted": len(deleted),
        "output": None if args.dry_run else out_path,
        "deleted_titles": [c.get("name") or c.get("uuid") for c in deleted][:50],
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
