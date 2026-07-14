#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# wiki-pr.sh — Branch + PR workflow for the Obsidian vault.
#
# Every vault update goes through a PR. This script enforces the branch-naming
# convention and produces the graph-impact PR body Vlad reviews.
#
#   wiki-pr.sh start  <workflow>
#       Create and check out a branch  <workflow>.<yyyy.mm.dd-hh.mm>  (in OBSIDIAN_TZ).
#       <workflow> MUST be listed in the vault's CLAUDE.md "Branch Workflow Registry"
#       — this keeps branch prefixes consistent. If it isn't, the script refuses and
#       tells you to add it there first.
#
#   wiki-pr.sh finish [--title T] [--needs-input "Q"]... [-m MSG] [--dry-run]
#       Stage all vault changes, commit, push, and open a PR whose body is generated
#       by wiki-graph-diff.py (sections: 0 needs-input, 1 notes, 2 edges, 3 tags).
#       --dry-run prints the branch + body and does not push or open a PR.
#
# Config is resolved the same way as the rest of the framework (.env → ~/.obsidian-wiki/config).
# ---------------------------------------------------------------------------

_find_config() {
  local dir="$PWD"
  while [[ "$dir" != "$HOME" && "$dir" != "/" ]]; do
    if [[ -f "$dir/.env" ]] && grep -q "OBSIDIAN_VAULT_PATH" "$dir/.env" 2>/dev/null; then
      echo "$dir/.env"; return
    fi
    dir="$(dirname "$dir")"
  done
  [[ -f "$HOME/.obsidian-wiki/config" ]] && echo "$HOME/.obsidian-wiki/config"
}

CFG="$(_find_config)"
[[ -z "${CFG:-}" ]] && { echo "wiki-pr: no config found (run wiki-setup)" >&2; exit 1; }
# shellcheck source=/dev/null
source "$CFG"
: "${OBSIDIAN_VAULT_PATH:?wiki-pr: OBSIDIAN_VAULT_PATH not set}"
VAULT="$OBSIDIAN_VAULT_PATH"
TZ_VAL="${OBSIDIAN_TZ:-UTC}"
REPO="${OBSIDIAN_WIKI_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"

git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "wiki-pr: $VAULT is not a git repository." >&2; exit 1; }

# Registry lives in the vault's CLAUDE.md (or AGENTS.md). Names are the backtick-quoted
# first token of each bullet under the "Branch Workflow Registry" heading.
_registry_file() {
  for f in "$VAULT/CLAUDE.md" "$VAULT/AGENTS.md"; do
    [[ -f "$f" ]] && { echo "$f"; return; }
  done
}

_registry_names() {
  local f; f="$(_registry_file)"; [[ -z "$f" ]] && return
  awk '
    /^#+[[:space:]]+Branch Workflow Registry/ {inreg=1; next}
    inreg && /^#+[[:space:]]/ {inreg=0}
    inreg && /^[[:space:]]*-[[:space:]]*`/ {
      line=$0; sub(/^[^`]*`/, "", line); sub(/`.*/, "", line); print line
    }
  ' "$f"
}

cmd="${1:-}"; shift || true

case "$cmd" in
  start)
    workflow="${1:-}"
    [[ -z "$workflow" ]] && { echo "usage: wiki-pr.sh start <workflow>" >&2; exit 1; }
    # Validate against the registry.
    if ! _registry_names | grep -qx "$workflow"; then
      echo "wiki-pr: '$workflow' is not in the Branch Workflow Registry." >&2
      echo "         Add it to $(_registry_file) under '## Branch Workflow Registry' first," >&2
      echo "         e.g.  - \`$workflow\` — <one-line purpose>" >&2
      echo "         Known workflows: $(_registry_names | paste -sd', ' -)" >&2
      exit 2
    fi
    stamp="$(TZ="$TZ_VAL" date +%Y.%m.%d-%H.%M)"
    branch="${workflow}.${stamp}"
    # Always branch off the latest remote main so a stale local main can't
    # base the branch on old history (which produces conflicting/regressive PRs).
    git -C "$VAULT" fetch origin --quiet 2>/dev/null \
      || echo "wiki-pr: warning: 'git fetch' failed — branching off local state" >&2
    base_ref="origin/main"
    git -C "$VAULT" rev-parse --verify "$base_ref" >/dev/null 2>&1 || base_ref="main"
    git -C "$VAULT" checkout -b "$branch" "$base_ref" || exit 1
    echo "$branch"
    ;;

  finish)
    title=""; msg=""; dry=0; needs=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --title) title="$2"; shift 2 ;;
        --needs-input) needs+=("$2"); shift 2 ;;
        -m|--message) msg="$2"; shift 2 ;;
        --dry-run) dry=1; shift ;;
        *) echo "wiki-pr: unknown arg '$1'" >&2; exit 1 ;;
      esac
    done

    branch="$(git -C "$VAULT" branch --show-current)"
    [[ "$branch" == "main" ]] && { echo "wiki-pr: refusing to finish on main — run 'start' first." >&2; exit 1; }
    workflow="${branch%%.*}"
    [[ -z "$title" ]] && title="${workflow}: vault update ($branch)"
    [[ -z "$msg" ]] && msg="$title"

    # Build the PR body from the graph diff (base = origin/main).
    body_file="$(mktemp -t wiki-pr-body.XXXXXX).md"
    ni_args=(); for q in "${needs[@]:-}"; do [[ -n "$q" ]] && ni_args+=(--needs-input "$q"); done
    python3 "$REPO/scripts/wiki-graph-diff.py" \
      --vault "$VAULT" --base "origin/main" --title "$title" --workflow "$workflow" \
      "${ni_args[@]}" > "$body_file" || { echo "wiki-pr: body generation failed" >&2; exit 1; }

    if [[ "$dry" == "1" ]]; then
      echo "── branch: $branch"
      echo "── commit: $msg"
      echo "── PR body ──────────────────────────────"
      cat "$body_file"
      echo "─────────────────────────────────────────"
      echo "(dry-run — nothing staged, pushed, or opened)"
      exit 0
    fi

    git -C "$VAULT" add -A
    if git -C "$VAULT" diff --cached --quiet; then
      echo "wiki-pr: no staged changes to commit." >&2; exit 1
    fi
    git -C "$VAULT" commit -m "$msg" || exit 1
    git -C "$VAULT" push -u origin "$branch" || exit 1
    ( cd "$VAULT" && gh pr create --base main --head "$branch" \
        --title "$title" --body-file "$body_file" )
    ;;

  *)
    echo "usage: wiki-pr.sh {start <workflow> | finish [--title T] [--needs-input Q]... [-m MSG] [--dry-run]}" >&2
    exit 1
    ;;
esac
