#!/usr/bin/env python3
"""
wiki-graph-diff.py — Generate a graph-impact PR body from a git diff of an Obsidian vault.

Compares a base ref (default origin/main) against the current working tree and emits a
Markdown PR body with the sections Vlad reviews:

  0. Needs user input   — placeholder for open questions (filled by the agent/skill)
  1. Notes created / updated — every changed .md with its `sources:` (data attribution)
  2. Edges created / removed — [[wikilinks]] and markdown links added or removed between notes
  3. Tags created            — tag values that did not exist anywhere in the vault at the base

Pure stdlib. Frontmatter is parsed with a minimal reader (no PyYAML dependency).

Usage:
  wiki-graph-diff.py --vault /path/to/vault [--base origin/main] [--title "..."] \
      [--workflow wiki-claude-export] [--needs-input "line one" --needs-input "line two"]

Writes the PR body to stdout.
"""
import argparse
import os
import re
import subprocess
import sys

# Vault infrastructure — not knowledge-graph notes. Excluded from note/edge/tag parsing.
NOTE_EXCLUDE_EXACT = {"index.md", "log.md", "hot.md", "CLAUDE.md", "AGENTS.md", "README.md"}


def is_note(path):
    """True if this .md path is a knowledge note (not infra, not a dotfile, not _meta)."""
    if not path.endswith(".md"):
        return False
    parts = path.split("/")
    if any(p.startswith(".") for p in parts):
        return False
    if path in NOTE_EXCLUDE_EXACT:
        return False
    if parts[0] == "_meta":
        return False
    return True


WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")
MDLINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+?\.md)\)")
TAG_INLINE_RE = re.compile(r"#([A-Za-z0-9_][A-Za-z0-9_/\-]*)")


def git(vault, *args):
    return subprocess.run(
        ["git", "-C", vault, *args],
        capture_output=True, text=True,
    )


def show(vault, ref, path):
    """Content of path at ref, or '' if it doesn't exist there."""
    r = git(vault, "show", f"{ref}:{path}")
    return r.stdout if r.returncode == 0 else ""


def read_head(vault, path):
    full = os.path.join(vault, path)
    if not os.path.isfile(full):
        return ""
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def split_frontmatter(text):
    """Return (frontmatter_str, body_str). Frontmatter is the block between leading --- fences."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    fm = text[3:end].strip("\n")
    body = text[end + 4:]
    return fm, body


def fm_list(fm, key):
    """Extract a YAML list value (block `- x` or inline `[a, b]`) for a top-level key."""
    out = []
    lines = fm.splitlines()
    for i, ln in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}\s*:\s*(.*)$", ln)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline.startswith("[") and inline.endswith("]"):
            out += [x.strip().strip("'\"") for x in inline[1:-1].split(",") if x.strip()]
        elif inline:
            out.append(inline.strip("'\""))
        # gather following indented `- item` lines
        for ln2 in lines[i + 1:]:
            m2 = re.match(r"^\s*-\s+(.*)$", ln2)
            if m2:
                out.append(m2.group(1).strip().strip("'\""))
            elif re.match(r"^\S", ln2):
                break
        break
    return [x for x in out if x]


def fm_scalar(fm, key):
    for ln in fm.splitlines():
        m = re.match(rf"^{re.escape(key)}\s*:\s*(.*)$", ln)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def note_label(path, text):
    title = fm_scalar(split_frontmatter(text)[0], "title")
    return title or os.path.splitext(os.path.basename(path))[0]


def outgoing_links(text):
    _, body = split_frontmatter(text)
    links = set(m.strip() for m in WIKILINK_RE.findall(body))
    links |= set(os.path.splitext(os.path.basename(m))[0] for m in MDLINK_RE.findall(body))
    return links


def all_tags(text):
    fm, body = split_frontmatter(text)
    tags = set(t.lower() for t in fm_list(fm, "tags"))
    # inline #tags in the body count too
    tags |= set(t.lower() for t in TAG_INLINE_RE.findall(body))
    return tags


def changed_md_files(vault, base):
    files = []
    seen = set()
    # Tracked changes vs base.
    r = git(vault, "diff", "--name-status", base, "--", "*.md")
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][0]  # A / M / D / R
        path = parts[-1]
        if is_note(path):
            files.append((status, path)); seen.add(path)
    # Untracked new notes (git diff ignores these) — treat as additions.
    u = git(vault, "ls-files", "--others", "--exclude-standard", "--", "*.md")
    for path in u.stdout.splitlines():
        if path and path not in seen and is_note(path):
            files.append(("A", path)); seen.add(path)
    return files


def vault_tags_at(vault, ref):
    """Union of all tags across every tracked .md file at a ref."""
    r = git(vault, "ls-tree", "-r", "--name-only", ref)
    tags = set()
    for path in r.stdout.splitlines():
        if is_note(path):
            tags |= all_tags(show(vault, ref, path))
    return tags


def worktree_tags(vault):
    """Union of all tags across every knowledge note currently on disk (tracked + untracked)."""
    tags = set()
    for root, dirs, names in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in (".git", ".obsidian", "node_modules")]
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), vault)
            if is_note(rel):
                try:
                    with open(os.path.join(root, n), "r", encoding="utf-8", errors="replace") as f:
                        tags |= all_tags(f.read())
                except OSError:
                    pass
    return tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--title", default="")
    ap.add_argument("--workflow", default="")
    ap.add_argument("--needs-input", action="append", default=[],
                    help="an open question requiring Vlad's input (repeatable)")
    args = ap.parse_args()

    vault = os.path.abspath(args.vault)
    base = args.base

    # Fall back to empty-tree if base is unreachable (e.g. brand-new repo).
    if git(vault, "rev-parse", "--verify", base).returncode != 0:
        base = git(vault, "hash-object", "-t", "tree", "/dev/null").stdout.strip() \
            or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

    files = changed_md_files(vault, base)

    created, updated, deleted = [], [], []
    edges_added, edges_removed = [], []

    for status, path in files:
        base_text = show(vault, base, path)
        head_text = read_head(vault, path)
        label = note_label(path, head_text or base_text)

        base_links = outgoing_links(base_text) if base_text else set()
        head_links = outgoing_links(head_text) if head_text else set()
        for tgt in sorted(head_links - base_links):
            if tgt != label:
                edges_added.append((label, tgt))
        for tgt in sorted(base_links - head_links):
            if tgt != label:
                edges_removed.append((label, tgt))

        if status == "A":
            created.append((path, label, fm_list(split_frontmatter(head_text)[0], "sources")))
        elif status == "D":
            deleted.append((path, label))
        else:  # M, R
            updated.append((path, label, fm_list(split_frontmatter(head_text)[0], "sources")))

    # New tags = tags present on disk now that did not exist anywhere at the base ref.
    new_tags = sorted(worktree_tags(vault) - vault_tags_at(vault, base))

    # ---- Emit PR body ----
    out = []
    title = args.title or (f"{args.workflow}: vault update" if args.workflow else "Vault update")
    out.append(f"# {title}\n")

    out.append("## 0. Needs user input\n")
    if args.needs_input:
        out += [f"- [ ] {q}" for q in args.needs_input]
    else:
        out.append("_None — nothing blocked on your input._")
    out.append("")

    out.append("## 1. Notes created / updated\n")
    if not created and not updated and not deleted:
        out.append("_No note changes._")
    else:
        if created:
            out.append("**Created**\n")
            for path, label, sources in created:
                src = ", ".join(sources) if sources else "⚠️ no `sources:` — attribution missing"
                out.append(f"- 🆕 `{path}` — **{label}**  \n  data attribution: {src}")
            out.append("")
        if updated:
            out.append("**Updated**\n")
            for path, label, sources in updated:
                src = ", ".join(sources) if sources else "⚠️ no `sources:`"
                out.append(f"- ✏️ `{path}` — **{label}**  \n  data attribution: {src}")
            out.append("")
        if deleted:
            out.append("**Deleted**\n")
            for path, label in deleted:
                out.append(f"- 🗑️ `{path}` — **{label}**")
            out.append("")

    out.append("## 2. Edges created / removed\n")
    if not edges_added and not edges_removed:
        out.append("_No link changes._")
    else:
        if edges_added:
            out.append("**Created**\n")
            out += [f"- `{s}` → `{t}`" for s, t in edges_added]
            out.append("")
        if edges_removed:
            out.append("**Removed**\n")
            out += [f"- `{s}` ✕→ `{t}`" for s, t in edges_removed]
            out.append("")

    out.append("## 3. Tags created\n")
    if new_tags:
        out += [f"- `#{t}`" for t in new_tags]
    else:
        out.append("_No new tags._")
    out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
