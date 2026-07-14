---
name: wiki-chat
description: >-
  Distill the CURRENT claude.ai session — the conversation itself plus any artifacts produced in
  it (documents, code, canvases, diagrams, plans) — into the Obsidian wiki, live, via the
  obsidian-wiki local MCP server. Use this skill when the user is running in a claude.ai desktop
  or cowork session connected to the obsidian-wiki MCP and says "/wiki-chat", "save this chat to
  my wiki", "document this session", "capture this conversation and its artifacts", "add what we
  just did to my brain", or "file this session". It writes/updates pages directly through the MCP
  vault tools (write_page, append_log, write_special, write_manifest) following the same
  distillation, provenance, dedup, and cross-linking rules as wiki-ingest/wiki-capture. It does
  NOT open a PR — the MCP server has no git tool, so writes land in the vault directly. Distinct
  from wiki-claude-export (bulk offline ingest of an unzipped data-export folder) and from
  wiki-capture (the CLI/Claude-Code capture that branches and opens a PR).
---

# Wiki Chat — Distill the Live claude.ai Session

You are running **inside a claude.ai desktop or cowork session** that is connected to the
**`obsidian-wiki` local MCP server**. The user wants to preserve this session in their wiki. The
source is not a file on disk — it is **the conversation you are in right now**, which you already
hold in context, together with any **artifacts you produced** during it (Claude Artifacts,
documents, code blocks, canvases, diagrams, plans, tables).

The job: distill this session into interconnected wiki pages and write them through the MCP's
vault tools — creating new pages and, wherever the knowledge already has a home, **updating
existing pages in place** rather than duplicating.

**No PR.** The obsidian-wiki MCP server exposes only vault I/O tools — there is no git/shell tool,
so you cannot run `wiki-pr.sh`. Writes go **directly into the vault**. This is the deliberate
trade-off for live desktop use; the CLI counterparts (`wiki-capture`, `wiki-claude-export`) are
the branch-and-PR path. Do not attempt to shell out or invent a PR step.

## Tools You Use

All vault I/O is through the `obsidian-wiki` MCP server — never assume a shell, filesystem, or
`git` is available:

| Need | Tool |
|---|---|
| Resolve vault path, link format, categories, current date | `resolve_config` |
| Cheap index pass (title/category/tags/summary) | `list_pages` |
| Full-text substring search for dedup / link targets | `search_vault` |
| Read a page before updating it | `read_page` |
| Create or update a page (validates frontmatter, stamps `updated`) | `write_page` |
| Read index / log / hot / taxonomy / manifest / conventions | `read_special` |
| Overwrite index / hot / insights / taxonomy | `write_special` |
| Append one activity-log line | `append_log` |
| Overwrite `.manifest.json` (read it first via `read_special "manifest"`) | `write_manifest` |

## Before You Start

1. Call `resolve_config` first. It returns `vault_path`, `link_format` (`wikilink` vs `markdown`),
   the category folders, timezone, and today's date. Use `link_format` to decide `[[wikilinks]]`
   vs standard Markdown links, and use the returned `now`/date for frontmatter.
2. Read the owner conventions via `read_special "conventions"` (the vault's `AGENTS.md`) and apply
   them — domain vocabulary, writing style, project scoping, ingest preferences. They override the
   defaults here.
3. Prime for dedup and linking: `read_special "hot"` (recent activity), and `read_special
   "taxonomy"` (controlled tag vocabulary). Prefer `list_pages`/`search_vault` over reading full
   pages until you know which ones you're touching.

## Step 1: Scope the Session

Decide what to capture. Default to the **substantive knowledge and artifacts of the whole
session**; skip pleasantries, dead ends, and back-and-forth that led nowhere.

- If the user named a slice ("just the auth design", "the migration script we wrote"), capture
  only that.
- If the session is broad, cluster it by topic — one page per topic, not one page per session.
- If nothing durable was produced (quick lookup, casual chat), say so and stop rather than
  manufacturing a page.

## Step 2: Distill Two Things — Substance AND Artifacts

This skill captures **both axes**. Treat them distinctly:

**A. The conversation substance** — the durable knowledge exchanged: decisions and their
rationale, problems solved and how, constraints discovered, mental models, gotchas, trade-offs.
Distill this as declarative present-tense knowledge, exactly as `wiki-capture`/`wiki-ingest` do —
**knowledge, not a transcript.** Do not paste turns of dialogue.

**B. The artifacts you produced** — anything you generated in the session (a Claude Artifact, a
code file, a spec/PRD, a canvas, a diagram, a config, a query). For each artifact worth keeping:
- Record **what it is, what it's for, and the key decisions embedded in it** — the reusable
  essence, not a re-paste of the whole thing.
- If the artifact *is itself* a reusable reference (a config snippet, a schema, a command, a
  canonical prompt), preserve that verbatim in a `references/` page inside a fenced block — that's
  the one case where near-verbatim content belongs.
- Cross-link the artifact page to the substance page(s) that explain *why* it exists, so the
  "what" and the "why" stay connected.

Route every page to the right folder per the standard taxonomy: `concepts/`, `entities/`,
`skills/`, `references/`, `synthesis/`, `journal/`, or `projects/<name>/`. Consult the taxonomy
(from Step 0) before assigning tags; keep to ≤5 domain/type tags (plus any `visibility/` system
tag, which doesn't count toward the limit).

## Step 3: Dedup, Then Write

**Compile, don't append.** For each topic/artifact, first check whether a page already covers it:
`list_pages` (by category/tag) and `search_vault` on the key terms. Then:

- **Existing page** → `read_page` it, merge the new knowledge in place (revise/extend, don't
  bolt on a dated log section), and `write_page` the full updated markdown.
- **New page** → `write_page` a fresh page. `write_page` requires the frontmatter keys `title`,
  `category`, `tags`, `sources`; it auto-fills `created` on new pages and always stamps `updated`
  to today. Also include `summary`, and — matching the other ingest skills — `base_confidence`,
  `lifecycle: draft`, `lifecycle_changed`, and a `provenance:` block.

**Sources / provenance.** This session has no export file and no guaranteed conversation UUID.
Cite the source as a claude.ai session, e.g. `sources: ["claude.ai session: <short title>
(<date>)"]`. If the user can give a conversation/share link or id, use it
(`claudeai:<id>`) so re-runs can dedup precisely.

**Cross-link** every new page to ≥2 related existing pages (`[[wikilinks]]`, or Markdown links
when `link_format` is `markdown`), and add a back-link from at least one strong existing page.

## Step 4: Privacy

- Distill and synthesize — never copy raw chat text verbatim (artifacts that are themselves
  reusable references, per Step 2B, are the deliberate exception).
- Skip anything resembling secrets, API keys, passwords, or tokens.
- Desktop sessions often touch personal or third-party content. If sensitive personal content
  appears, ask before including it, and consider a `visibility/pii` or `visibility/internal` tag.

## Step 5: Track (directly, no PR)

After the page writes, keep the maintained files current — the MCP server does **not** do this for
you:

- **`.manifest.json`** — `read_special "manifest"`, add/update one entry for this session (key it
  by the conversation id if you have one, else a `session:<slug>-<date>` key), with
  `source_type: "claudeai_session"`, `title`, `captured_at`, `pages_created`, `pages_updated`,
  and bump `last_updated`; then `write_manifest` the full JSON.
- **`index.md`** — `read_special "index"`, add any new pages under their category sections, then
  `write_special "index"`.
- **`log.md`** — `append_log` one line, e.g.
  `WIKI_CHAT session="<title>" pages_created=X pages_updated=Y artifacts=Z` (it timestamps and
  prefixes `- ` for you — pass the bare entry).
- **`hot.md`** — `read_special "hot"`, update **Recent Activity** (one line, e.g. "Captured live
  claude.ai session '<title>' — N pages, M artifacts") and refresh the `updated:` timestamp, then
  `write_special "hot"`.

## Step 6: Report

```
## /wiki-chat  (live session → vault, no PR)

Session:   <short title>            (source: claude.ai session, <date>)
Substance: 3 topics distilled
Artifacts: 2 preserved (1 reference page, 1 summarized)
Pages:     4 created, 2 updated
Tracked:   index.md, log.md, hot.md, .manifest.json updated
```

Remind the user this wrote **directly to the vault** (no PR was opened). If they want the change
reviewed as a PR, they can run `wiki-lint` / a manual `wiki-pr.sh` pass from a shell later, or use
the CLI `wiki-capture` next time.

## QMD Refresh

The obsidian-wiki MCP server has no shell, so this skill **cannot** run a `qmd` refresh. If the
user maintains a QMD search index, tell them it may be stale until they run `qmd update` (and
`qmd embed` if prompted) themselves from a terminal. Do not attempt to invoke it.
