"""MCP server exposing the Obsidian wiki to MCP clients (e.g. Claude Desktop).

The wiki framework is *instructions*, not code: each skill is a ``SKILL.md`` the
model executes against filesystem primitives. This server mirrors that split:

* **Tools** — deterministic vault I/O, sandboxed to ``OBSIDIAN_VAULT_PATH``
  (list/read/write pages, search, read/write the special files + manifest).
  ``write_page`` validates the required frontmatter and stamps ``updated`` so
  the vault's invariants survive writes that originate from a chat client.
* **Prompts** — one per bundled skill, sourced from its ``SKILL.md`` body. A
  client invokes ``wiki-ingest`` (etc.) and the model then executes the skill's
  instructions using the tools above — exactly how Claude Code runs a skill.
* **Resources** — the conventions (``AGENTS.md``), ``index.md``, taxonomy and
  ``hot.md``, attachable for context.

Run it over stdio (the transport Claude Desktop uses for local servers):

    obsidian-wiki mcp

This module imports ``mcp`` lazily-friendly: importing it requires the optional
``[mcp]`` extra (``pip install 'obsidian-wiki[mcp]'``).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import Prompt

# Reuse the installer's data + config resolution so the server and the CLI agree
# on where skills and the vault live.
from obsidian_wiki.cli import GLOBAL_CONFIG, bootstrap_dir, skills_dir

HOME = Path.home()

# Top-level files the wiki maintains by hand; never treated as content pages.
SPECIAL_FILES = {
    "index": "index.md",
    "log": "log.md",
    "hot": "hot.md",
    "insights": "_insights.md",
    "taxonomy": "_meta/taxonomy.md",
    "manifest": ".manifest.json",
    "conventions": "AGENTS.md",
}
# Required frontmatter on every content page (per the framework spec).
REQUIRED_FRONTMATTER = ("title", "category", "tags", "sources", "created", "updated")


# ── Config resolution ─────────────────────────────────────────────────────────
def _config_value(key: str) -> str:
    """Read KEY from ~/.obsidian-wiki/config (quotes stripped); '' if absent."""
    if not GLOBAL_CONFIG.is_file():
        return ""
    for line in GLOBAL_CONFIG.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _expand(raw: str) -> Path:
    """Expand ~ and env vars; resolve a relative path against $HOME."""
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = HOME / p
    return p.resolve()


def resolve_vault() -> Path:
    """Locate the vault: $OBSIDIAN_VAULT_PATH wins, else the global config."""
    raw = os.environ.get("OBSIDIAN_VAULT_PATH") or _config_value("OBSIDIAN_VAULT_PATH")
    if not raw or raw == "/path/to/your/vault":
        raise RuntimeError(
            "OBSIDIAN_VAULT_PATH is not set. Set it in the MCP server's `env` "
            "block, or run `obsidian-wiki setup --vault /path/to/vault`."
        )
    return _expand(raw)


def _sources_root() -> Path | None:
    raw = os.environ.get("OBSIDIAN_SOURCES_DIR") or _config_value("OBSIDIAN_SOURCES_DIR")
    return _expand(raw) if raw else None


def _now() -> datetime:
    """Current time in OBSIDIAN_TZ (IANA name); machine-local if unset/invalid.

    Never UTC by default — the vault stamps wall-clock times in the configured
    zone. See the Timestamp Convention in llm-wiki/SKILL.md.
    """
    tz_name = os.environ.get("OBSIDIAN_TZ") or _config_value("OBSIDIAN_TZ")
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:  # unknown zone name → fall back to local
            pass
    return datetime.now().astimezone()


# ── Path safety ────────────────────────────────────────────────────────────────
def _safe(root: Path, rel: str) -> Path:
    """Resolve REL under ROOT, refusing escapes outside the sandbox."""
    rel = rel.strip().lstrip("/")
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"path escapes the vault sandbox: {rel}")
    return target


def _now_date() -> str:
    return _now().strftime("%Y-%m-%d")


# ── Minimal frontmatter handling (no YAML dependency) ───────────────────────────
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block, body). frontmatter_block is None if absent."""
    m = _FM_RE.match(text)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def _top_level_keys(fm: str) -> set[str]:
    return {
        m.group(1)
        for line in fm.splitlines()
        if (m := re.match(r"^([A-Za-z0-9_-]+):", line))
    }


def _scalar(fm: str, key: str) -> str:
    """Best-effort read of an inline scalar value for KEY (else '')."""
    for line in fm.splitlines():
        m = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def _tags(fm: str) -> list[str]:
    """Extract tags whether written inline ([a, b]) or as a block list."""
    inline = _scalar(fm, "tags")
    if inline.startswith("["):
        return [t.strip().strip('"').strip("'") for t in inline.strip("[]").split(",") if t.strip()]
    out: list[str] = []
    grabbing = False
    for line in fm.splitlines():
        if re.match(r"^tags:\s*$", line):
            grabbing = True
            continue
        if grabbing:
            m = re.match(r"^\s+-\s*(.+)$", line)
            if m:
                out.append(m.group(1).strip().strip('"').strip("'"))
            elif line and not line.startswith(" "):
                break
    return out


def _set_or_insert(fm: str, key: str, value: str) -> str:
    """Replace KEY's inline value, or insert `key: value` at the end."""
    pattern = re.compile(rf"^({re.escape(key)}):.*$", re.MULTILINE)
    if pattern.search(fm):
        return pattern.sub(f"{key}: {value}", fm, count=1)
    sep = "" if fm.endswith("\n") or not fm else "\n"
    return f"{fm}{sep}{key}: {value}"


# ── Skill prompt metadata ───────────────────────────────────────────────────────
def _skill_meta(text: str) -> tuple[str, str]:
    """Pull (name, description) from a SKILL.md frontmatter, folding scalars."""
    fm, _ = _split_frontmatter(text)
    if fm is None:
        return "", ""
    name = _scalar(fm, "name")
    desc = _scalar(fm, "description")
    if desc in (">", "|", ">-", "|-", ">+", "|+"):  # folded/literal block scalar
        lines: list[str] = []
        grabbing = False
        for line in fm.splitlines():
            if re.match(r"^description:", line):
                grabbing = True
                continue
            if grabbing:
                if line.strip() == "" or line.startswith((" ", "\t")):
                    lines.append(line.strip())
                else:
                    break
        desc = " ".join(x for x in lines if x)
    return name, re.sub(r"\s+", " ", desc).strip()[:1500]


# ── Server construction ─────────────────────────────────────────────────────────
def build_server() -> FastMCP:
    mcp = FastMCP(
        "obsidian-wiki",
        instructions=(
            "Read/write a local Obsidian LLM-Wiki vault. To perform a wiki "
            "operation (ingest, query, lint, capture, update, ...), first fetch "
            "the matching skill PROMPT — its body contains the full procedure — "
            "then carry it out using these tools. Always keep index.md, log.md, "
            "hot.md and .manifest.json current after writes, per the skill."
        ),
    )

    # ── Tools: deterministic vault I/O ──────────────────────────────────────────
    @mcp.tool()
    def resolve_config() -> dict:
        """Return the resolved vault path, link format, and category folders.

        Call this first so subsequent paths are relative to the right vault.
        """
        vault = resolve_vault()
        cats = sorted(
            p.name
            for p in vault.iterdir()
            if p.is_dir() and not p.name.startswith((".", "_"))
        ) if vault.is_dir() else []
        return {
            "vault_path": str(vault),
            "vault_exists": vault.is_dir(),
            "link_format": os.environ.get("OBSIDIAN_LINK_FORMAT")
            or _config_value("OBSIDIAN_LINK_FORMAT")
            or "wikilink",
            "sources_dir": str(_sources_root()) if _sources_root() else None,
            "timezone": str(_now().tzinfo),
            "now": _now().strftime("%Y-%m-%d %H:%M"),
            "categories": cats,
            "special_files": list(SPECIAL_FILES),
        }

    @mcp.tool()
    def list_pages(category: str | None = None, tag: str | None = None) -> list[dict]:
        """List content pages with title/category/tags/summary from frontmatter.

        This is the cheap "index pass": prefer it over reading full pages.
        Optionally filter by CATEGORY (folder) or TAG.
        """
        vault = resolve_vault()
        out: list[dict] = []
        for md in sorted(vault.rglob("*.md")):
            rel = md.relative_to(vault)
            if rel.parts[0].startswith((".",)):
                continue
            if rel.name in {"index.md", "log.md", "hot.md", "_insights.md"} and len(rel.parts) == 1:
                continue
            cat = rel.parts[0] if len(rel.parts) > 1 else ""
            if category and cat != category:
                continue
            fm, _ = _split_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            tags = _tags(fm) if fm else []
            if tag and tag not in tags:
                continue
            out.append({
                "path": str(rel),
                "title": (_scalar(fm, "title") if fm else "") or md.stem,
                "category": (_scalar(fm, "category") if fm else "") or cat,
                "tags": tags,
                "summary": _scalar(fm, "summary") if fm else "",
                "updated": _scalar(fm, "updated") if fm else "",
            })
        return out

    @mcp.tool()
    def read_page(path: str) -> str:
        """Read a single page (full markdown incl. frontmatter). PATH is vault-relative."""
        target = _safe(resolve_vault(), path)
        if not target.is_file():
            raise FileNotFoundError(f"no such page: {path}")
        return target.read_text(encoding="utf-8")

    @mcp.tool()
    def write_page(path: str, content: str) -> dict:
        """Create or overwrite a content page. PATH is vault-relative (e.g.
        'concepts/foo.md'); CONTENT is the full markdown including YAML
        frontmatter.

        Enforces the vault invariants: the required frontmatter keys
        (title, category, tags, sources, created, updated) must be present,
        `created` is filled on new pages, and `updated` is always stamped to
        today. Does NOT touch index/log/hot/manifest — call append_log and
        write_special per the skill so placement stays semantic.
        """
        vault = resolve_vault()
        target = _safe(vault, path)
        if target.suffix != ".md":
            raise ValueError("write_page only writes .md pages")

        fm, body = _split_frontmatter(content)
        if fm is None:
            raise ValueError("page is missing YAML frontmatter (--- block at top)")
        keys = _top_level_keys(fm)
        is_new = not target.exists()

        missing = [k for k in REQUIRED_FRONTMATTER if k not in keys]
        # created/updated are auto-managed, so don't reject on those.
        hard_missing = [k for k in missing if k not in ("created", "updated")]
        if hard_missing:
            raise ValueError(f"frontmatter missing required keys: {', '.join(hard_missing)}")

        today = _now_date()
        if "created" not in keys:
            fm = _set_or_insert(fm, "created", today)
        fm = _set_or_insert(fm, "updated", today)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
        return {
            "path": str(target.relative_to(vault)),
            "created_new": is_new,
            "updated": today,
            "reminder": "Now update index.md (if new), append to log.md, and refresh hot.md.",
        }

    @mcp.tool()
    def search_vault(query: str, max_results: int = 40) -> list[dict]:
        """Case-insensitive substring search over page titles and bodies.

        Returns matches with a short snippet around the first hit.
        """
        vault = resolve_vault()
        q = query.lower()
        out: list[dict] = []
        for md in sorted(vault.rglob("*.md")):
            if md.relative_to(vault).parts[0].startswith("."):
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
            idx = text.lower().find(q)
            if idx == -1:
                continue
            start, end = max(0, idx - 80), min(len(text), idx + 160)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            out.append({"path": str(md.relative_to(vault)), "snippet": f"…{snippet}…"})
            if len(out) >= max_results:
                break
        return out

    @mcp.tool()
    def read_special(name: str) -> str:
        """Read a maintained file by NAME: one of index, log, hot, insights,
        taxonomy, manifest, conventions."""
        vault = resolve_vault()
        if name not in SPECIAL_FILES:
            raise ValueError(f"unknown special file '{name}'; choose from {list(SPECIAL_FILES)}")
        target = vault / SPECIAL_FILES[name]
        if not target.is_file():
            if name == "conventions":  # fall back to the bundled framework AGENTS.md
                boot = bootstrap_dir()
                cand = (boot / "AGENTS.md") if boot else None
                if cand and cand.is_file():
                    return cand.read_text(encoding="utf-8")
            return f"(‹{name}› does not exist yet at {target})"
        return target.read_text(encoding="utf-8")

    @mcp.tool()
    def write_special(name: str, content: str) -> dict:
        """Overwrite a maintained text file: index, hot, insights, or taxonomy.
        Use append_log for log.md and write_manifest for the manifest."""
        if name not in {"index", "hot", "insights", "taxonomy"}:
            raise ValueError("write_special handles only index, hot, insights, taxonomy")
        vault = resolve_vault()
        target = vault / SPECIAL_FILES[name]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": str(target.relative_to(vault)), "bytes": len(content.encode())}

    @mcp.tool()
    def append_log(entry: str) -> dict:
        """Append one timestamped line to log.md (the activity log)."""
        vault = resolve_vault()
        target = vault / "log.md"
        stamp = _now().strftime("%Y-%m-%d %H:%M")
        line = entry.strip().lstrip("- ").strip()
        with target.open("a", encoding="utf-8") as f:
            f.write(f"- {stamp} — {line}\n")
        return {"appended": f"{stamp} — {line}"}

    @mcp.tool()
    def write_manifest(content: str) -> dict:
        """Overwrite .manifest.json. CONTENT must be valid JSON (validated here)."""
        vault = resolve_vault()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"manifest is not valid JSON: {exc}") from exc
        target = vault / ".manifest.json"
        target.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
        return {"sources": len(parsed.get("sources", {})), "path": ".manifest.json"}

    @mcp.tool()
    def read_source(path: str) -> str:
        """Read a raw source for ingestion. Allowed only inside the vault's
        _raw/ staging area or under $OBSIDIAN_SOURCES_DIR (if configured)."""
        vault = resolve_vault()
        candidate = Path(os.path.expanduser(path)).resolve()
        roots = [vault]
        src = _sources_root()
        if src:
            roots.append(src)
        if not any(r == candidate or r in candidate.parents for r in roots):
            allowed = ", ".join(str(r) for r in roots)
            raise ValueError(f"source path must be within: {allowed}")
        if not candidate.is_file():
            raise FileNotFoundError(f"no such source: {path}")
        return candidate.read_text(encoding="utf-8", errors="replace")

    # ── Prompts: one per bundled skill ──────────────────────────────────────────
    registered = 0
    for skill_dir in sorted(p for p in skills_dir().iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        text = skill_md.read_text(encoding="utf-8")
        name, desc = _skill_meta(text)
        name = name or skill_dir.name
        mcp.add_prompt(
            Prompt.from_function(
                (lambda body=text: body),  # capture per-iteration body
                name=name,
                description=desc or f"Run the {name} wiki skill.",
            )
        )
        registered += 1

    # ── Resources: attachable context ───────────────────────────────────────────
    @mcp.resource("wiki://conventions", name="Wiki conventions (AGENTS.md)")
    def _conventions() -> str:
        return read_special("conventions")

    @mcp.resource("wiki://index", name="Wiki index")
    def _index() -> str:
        return read_special("index")

    @mcp.resource("wiki://hot", name="Wiki hot cache")
    def _hot() -> str:
        return read_special("hot")

    @mcp.resource("wiki://taxonomy", name="Tag taxonomy")
    def _taxonomy() -> str:
        return read_special("taxonomy")

    return mcp


def serve() -> None:
    """Build and run the server over stdio (Claude Desktop's local transport)."""
    build_server().run()
