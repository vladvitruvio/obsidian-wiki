---
name: wiki-chat
description: >-
  Ingest claude.ai *web app* conversations into the Obsidian wiki from an unzipped data-export
  folder. Use this skill when the user says "/wiki-chat", "ingest my claude.ai chats", "process
  my Claude export", "sync my claude.ai conversations", or "add my web chats to the wiki". The
  user points the skill at their unzipped claude.ai export folder (containing conversations.json
  + users.json); the skill uses users.json to DELETE every conversation in conversations.json
  that does not belong to the account owner (default "Vlad Munteanu"), then ingests only the
  owner's chats via the standard wiki distillation. This is the manual, cost-controlled
  counterpart to automated history ingest — it runs only when invoked. Distinct from
  claude-history-ingest (that mines the LOCAL ~/.claude CLI transcripts; this handles the
  claude.ai web app, which has no local files and no public API, only the manual data export).
---

# Wiki Chat — Ingest claude.ai Web Conversations

You ingest the user's **claude.ai web app** conversations into the Obsidian wiki. These live on
Anthropic's servers with no public list/export API, so the only source is the user's manual data
export (claude.ai → Settings → Privacy → **Export data**), which arrives as an emailed zip the
user unzips themselves.

The job: given the path to that **unzipped export folder**, use `users.json` to **delete every
conversation in `conversations.json` that isn't the account owner's**, then distill the owner's
chats into wiki pages — deduped so re-runs are safe.

## Before You Start

1. **Resolve config** — follow the Config Resolution Protocol in `llm-wiki/SKILL.md` (walk up CWD
   for `.env` → `~/.obsidian-wiki/config`). This gives `OBSIDIAN_VAULT_PATH` and
   `OBSIDIAN_LINK_FORMAT`. Then read `$OBSIDIAN_VAULT_PATH/AGENTS.md` if present.
2. Resolve `CLAUDEAI_ACCOUNT_NAME` (config value → default `"Vlad Munteanu"`) — the owner whose
   chats are kept. Everything else in the export is deleted.
3. Read `.manifest.json`, `index.md`, and `hot.md` at the vault root — you dedup against the
   manifest and avoid re-creating pages that already exist.

`SKILL_DIR` below is this skill's own directory (where `filter_export.py` lives).

## Step 1: Get the Export Folder

The user **points this skill at their unzipped claude.ai export folder** — pass it as the argument
to `/wiki-chat`, or ask for it. Do not scan or guess; require an explicit path.

- If no path was given, ask: "Point me at your unzipped claude.ai export folder (the one containing
  `conversations.json` and `users.json`). Export it from claude.ai → **Settings → Privacy → Export
  data**, unzip it, and give me the folder path."
- Verify the folder contains `conversations.json` and `users.json`. If `conversations.json` is
  missing, stop and say so. If only `users.json` is missing, warn — the owner filter (Step 2) will
  fall back and may refuse.

```bash
EXPORT_DIR="<path the user gave>"
ls "$EXPORT_DIR"/conversations.json "$EXPORT_DIR"/users.json
```

## Step 2: Delete Non-Owner Conversations

Run the filter helper against the folder. It resolves the owner's account uuid from the sibling
`users.json` (matching `full_name` to `CLAUDEAI_ACCOUNT_NAME`), keeps only those conversations, and
**deletes the rest from `conversations.json` in place**:

```bash
python3 "$SKILL_DIR/filter_export.py" \
  --input "$EXPORT_DIR/conversations.json" \
  --owner "${CLAUDEAI_ACCOUNT_NAME:-Vlad Munteanu}" \
  --in-place
```

Read the JSON summary it prints (`total`, `kept`, `deleted`, `method`, `deleted_titles`).

**Safety gates — do not proceed blindly:**
- If `status` is `"unresolved"` (exit 3): the owner could not be identified and **nothing was
  deleted** (`conversations.json` is untouched). Do NOT guess. Show the user the schema (`head` of
  the file, keys of the first conversation, contents of `users.json`) and ask them to confirm the
  owner name or point you at the right field. Only continue once you can filter deterministically.
- If `deleted > 0`, report the count and a few `deleted_titles` so the deletion is visible, e.g.
  "Deleted 12 conversations not under Vlad Munteanu; kept 84." The dropped conversations are gone
  from `conversations.json` and never enter the vault.
- If `kept == 0`, stop — there is nothing to ingest; surface why.

From here on, `conversations.json` contains only the owner's chats.

## Step 3: Ingest the Owner's Conversations

Distill the kept conversations exactly as `wiki-ingest` handles raw/unstructured chat exports —
**do not** reinvent the distillation rules; follow `wiki-ingest/SKILL.md` (raw chat-export mode).
Key points that apply here:

- **Dedup by conversation `uuid`.** Check `.manifest.json` for each conversation's uuid under a
  `claudeai_conversation` source entry. Skip ones already ingested (append mode); only process new
  or changed (`updated_at` newer than the recorded ingest) conversations. Report sampled vs skipped.
- **Parse each conversation**: iterate `chat_messages[]`, using `sender` (`human`/`assistant`) and
  `text` (fall back to concatenating `content[].text` blocks when `text` is empty). `name` is the
  chat title; `created_at`/`updated_at` give timing.
- **Cluster by topic, not by chat.** Group knowledge across conversations; one page per topic.
  Route to `concepts/`, `entities/`, `skills/`, `references/`, `synthesis/`, or `projects/<name>/`
  per the standard taxonomy. Consult `tag-taxonomy` before assigning tags.
- **Distill knowledge, not transcript.** Write declarative present-tense knowledge with provenance
  markers (`^[inferred]`, `^[ambiguous]`). Every page gets the required frontmatter (`title`,
  `category`, `tags`, `sources`, `created`, `updated`, `summary`) plus `base_confidence`,
  `lifecycle: draft`, `lifecycle_changed`, and a `provenance:` block. Cite sources as
  `claudeai:<conversation-uuid>` or `"claude.ai chat: <title> (<date>)"`.
- **Cross-link** every new page to ≥2 existing pages (`[[wikilinks]]`, or Markdown links when
  `OBSIDIAN_LINK_FORMAT=markdown`).

## Step 4: Privacy

- Distill and synthesize — never copy raw chat text verbatim.
- Skip anything resembling secrets, API keys, passwords, or tokens.
- Web chats often touch personal or third-party content. If sensitive personal content appears, ask
  the user before including it, and consider a `visibility/pii` or `visibility/internal` tag.

## Step 5: Track

- **`.manifest.json`** — add/update one entry per ingested conversation: key it by uuid, with
  `source_type: "claudeai_conversation"`, `title`, `created_at`, `updated_at`, `ingested_at`,
  `pages_created`, `pages_updated`. Update `last_updated`.
- **`index.md`** — add new pages under their category sections.
- **`log.md`** — append:
  `- [TIMESTAMP] WIKI_CHAT kept=N deleted=M ingested=I pages_created=X pages_updated=Y`
- **`hot.md`** — update **Recent Activity** (one line, e.g. "Ingested N claude.ai web chats;
  deleted M non-owner chats") and bump the `updated:` frontmatter timestamp.

Do **not** move or archive the export folder — leave it wherever the user put it. (The manifest
deduping by conversation uuid is what makes re-runs safe, not archiving.)

## Step 6: Report

```
## /wiki-chat

Export:   <export folder path>
Owner:    Vlad Munteanu  (matched via users.json account uuid)
Filtered: 96 total → 84 kept, 12 deleted (not under owner)
Ingested: 9 new conversations (75 already in manifest, skipped)
Pages:    4 created, 6 updated
```

## QMD Refresh After Vault Writes

QMD is a search index, not the source of truth. If `$QMD_WIKI_COLLECTION` is empty or unset, skip
this step. Run it only after this skill has written vault markdown. If it fails, do not roll back
vault changes; report QMD status separately. Use `$QMD_CLI` if set, else `qmd`:

```bash
${QMD_CLI:-qmd} update
# if it reports embeddings are stale:
${QMD_CLI:-qmd} embed
```

Record one of: `QMD refreshed: update + embed + verified` · `QMD refreshed: update only +
verified` · `QMD skipped: QMD_WIKI_COLLECTION unset` · `QMD skipped: qmd CLI unavailable` · `QMD
failed: <short error>`.
