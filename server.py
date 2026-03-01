#!/usr/bin/env python3
"""
Obsidian MCP Server
Gives Claude full access to your Obsidian vault via the CLI.
Configure paths in .env (see .env.example).
"""

import asyncio
import subprocess
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

def log(msg: str):
    """Log to stderr (visible in Claude Desktop's MCP logs)."""
    print(msg, file=sys.stderr, flush=True)

VAULT_PATH = os.environ.get("VAULT_PATH", "")
SYSTEM_FOLDER = os.environ.get("SYSTEM_FOLDER", "System")

if not VAULT_PATH:
    log("ERROR: VAULT_PATH not set. Create a .env file — see .env.example.")
    sys.exit(1)

app = Server("obsidian-mcp")

OBSIDIAN_BIN = os.environ.get("OBSIDIAN_BIN", "/Applications/Obsidian.app/Contents/MacOS/obsidian")

# Build a clean environment for the Obsidian CLI.
# Claude Desktop strips most env vars, but Obsidian's CLI needs HOME and
# a reasonable PATH to communicate with the running Obsidian app.
_CLI_ENV = {
    **os.environ,
    "HOME": os.path.expanduser("~"),
    "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    "PATH": "/Applications/Obsidian.app/Contents/MacOS:/opt/anaconda3/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}

async def run_obsidian(args: list[str]) -> str:
    """Run an obsidian CLI command and return output."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            OBSIDIAN_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_CLI_ENV,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0 and stderr:
            return f"Error: {stderr.decode().strip()}"
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return "Error: obsidian CLI timed out (30s). Make sure Obsidian app is open."
    except Exception as e:
        return f"Error running obsidian CLI: {e}"


# ─────────────────────────────────────────────
# FILESYSTEM FALLBACKS for CLI commands that hang
# (search, tasks, daily:read, daily:append)
# ─────────────────────────────────────────────

def _vault_path() -> Path:
    return Path(VAULT_PATH)


def fs_search(query: str, folder: str | None = None, limit: int = 20) -> str:
    """Search vault files by grepping markdown content (fallback for CLI search)."""
    log(f"fs_search: starting query='{query}' folder={folder}")
    root = _vault_path() / folder if folder else _vault_path()
    log(f"fs_search: root={root}")
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    file_count = 0
    for p in sorted(root.rglob("*.md")):
        if ".obsidian" in p.parts or ".trash" in p.parts or ".git" in p.parts:
            continue
        file_count += 1
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            log(f"fs_search: error reading {p}: {e}")
            continue
        matches = list(pattern.finditer(text))
        if matches:
            rel = p.relative_to(_vault_path())
            lines = text.splitlines()
            snippets = []
            for m in matches[:3]:
                line_no = text[:m.start()].count("\n")
                start = max(0, line_no - 1)
                end = min(len(lines), line_no + 2)
                snippets.append(f"  L{line_no+1}: " + "\n  ".join(lines[start:end]))
            results.append(f"{rel} ({len(matches)} match{'es' if len(matches)>1 else ''}):\n" + "\n".join(snippets))
        if len(results) >= limit:
            break
    return "\n\n".join(results) if results else "No results found."


def fs_tasks(filter_val: str = "todo", file: str | None = None) -> str:
    """List tasks by scanning markdown files for checkboxes."""
    root = _vault_path()
    if file:
        # Find the file
        candidates = list(root.rglob(f"*{file}*"))
        files = [c for c in candidates if c.suffix == ".md"]
    else:
        files = sorted(root.rglob("*.md"))

    tasks = []
    for p in files:
        if ".obsidian" in p.parts or ".trash" in p.parts or ".git" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = p.relative_to(root)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ] ") and filter_val in ("todo", "all"):
                tasks.append(f"[ ] {stripped[6:]}  ({rel})")
            elif stripped.startswith("- [x] ") and filter_val in ("done", "all"):
                tasks.append(f"[x] {stripped[6:]}  ({rel})")
    return "\n".join(tasks) if tasks else "No tasks found."


def fs_daily_read() -> str:
    """Read today's daily note from the filesystem."""
    today = datetime.now().strftime("%Y-%m-%d")
    # Daily notes live in Journal/ folder
    path = _vault_path() / "Journal" / f"{today}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    # Fallback: search for it anywhere
    for p in _vault_path().rglob(f"{today}.md"):
        if ".obsidian" not in p.parts and ".trash" not in p.parts and ".git" not in p.parts:
            return p.read_text(encoding="utf-8")
    return "Daily note not found for today."


def fs_daily_append(content: str) -> str:
    """Append to today's daily note, creating it if needed."""
    today = datetime.now().strftime("%Y-%m-%d")
    # Daily notes live in Journal/ folder
    daily_path = _vault_path() / "Journal" / f"{today}.md"
    if not daily_path.is_file():
        # Search for it elsewhere
        for p in _vault_path().rglob(f"{today}.md"):
            if ".obsidian" not in p.parts and ".trash" not in p.parts and ".git" not in p.parts:
                daily_path = p
                break
    if not daily_path.is_file():
        daily_path.write_text(f"# {today}\n", encoding="utf-8")
    with open(daily_path, "a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended to {daily_path.relative_to(_vault_path())}"

# ─────────────────────────────────────────────
# TOOL DEFINITIONS
# ─────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="vault_search",
            description="Search the entire Obsidian vault for any text query. Uses Obsidian's live search index.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "folder": {"type": "string", "description": "Optional: limit search to a folder"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="read_note",
            description="Read the full contents of a note by name or path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Note name (e.g. 'Lecture 3') or path (e.g. '2026/Math/Lecture3.md')"},
                },
                "required": ["file"]
            }
        ),
        Tool(
            name="create_note",
            description="Create a new note in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Note name"},
                    "path": {"type": "string", "description": "Full path including folder, e.g. 'System/Goals.md'"},
                    "content": {"type": "string", "description": "Note content in markdown"},
                    "overwrite": {"type": "boolean", "description": "Overwrite if exists (default false)"}
                },
                "required": ["name", "content"]
            }
        ),
        Tool(
            name="append_to_note",
            description="Append content to an existing note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Note name or path"},
                    "content": {"type": "string", "description": "Content to append"}
                },
                "required": ["file", "content"]
            }
        ),
        Tool(
            name="get_backlinks",
            description="Get all notes that link to a given note. Useful for seeing how ideas connect.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Note name"}
                },
                "required": ["file"]
            }
        ),
        Tool(
            name="get_outgoing_links",
            description="Get all notes that a given note links to.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Note name"}
                },
                "required": ["file"]
            }
        ),
        Tool(
            name="list_orphan_notes",
            description="List all notes with no incoming links — disconnected notes that should probably be connected to something.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="list_all_tasks",
            description="List all tasks across the entire vault. Can filter by done/todo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "enum": ["todo", "done", "all"], "description": "Filter tasks (default: todo)"},
                    "file": {"type": "string", "description": "Optional: limit to a specific note"}
                }
            }
        ),
        Tool(
            name="get_daily_note",
            description="Read today's daily note.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="append_to_daily",
            description="Append content to today's daily note. Creates it if it doesn't exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Content to append"}
                },
                "required": ["content"]
            }
        ),
        Tool(
            name="list_vault_files",
            description="List files in the vault, optionally filtered by folder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Optional folder path to filter by"}
                }
            }
        ),
        Tool(
            name="list_tags",
            description="List all tags used in the vault with counts.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_vault_info",
            description="Get overall vault statistics — file count, folder count, size.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="evening_reflection",
            description="Run the evening reflection flow. Prompts the user with questions and writes a summary to today's daily note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "responses": {
                        "type": "object",
                        "description": "User's answers to reflection questions",
                        "properties": {
                            "accomplished": {"type": "string"},
                            "time_wasted": {"type": "string"},
                            "tomorrow_priority": {"type": "string"},
                            "brain_dump": {"type": "string"},
                            "energy_level": {"type": "string", "enum": ["low", "medium", "high"]}
                        }
                    }
                },
                "required": ["responses"]
            }
        ),
        Tool(
            name="update_goals",
            description="Read or update the Goals note with long-term, monthly, weekly goals.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "update"]},
                    "content": {"type": "string", "description": "New goals content (required if action=update)"}
                },
                "required": ["action"]
            }
        ),
        Tool(
            name="reading_queue",
            description="Read or add items to the Reading Queue note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "add", "mark_done"]},
                    "item": {"type": "string", "description": "Article/resource to add or mark done"}
                },
                "required": ["action"]
            }
        ),
        Tool(
            name="recurring_tasks",
            description="Read or update the Recurring Tasks note which stores how long common tasks take.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "add", "update_time"]},
                    "task": {"type": "string", "description": "Task name"},
                    "estimated_minutes": {"type": "integer", "description": "How long this task usually takes in minutes"}
                },
                "required": ["action"]
            }
        ),
    ]


# ─────────────────────────────────────────────
# TOOL HANDLERS
# ─────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
  try:
    log(f"Tool called: {name}")
    if name == "vault_search":
        result = fs_search(
            arguments["query"],
            arguments.get("folder"),
            arguments.get("limit", 20),
        )
        log(f"vault_search done, {len(result)} chars")
        return [TextContent(type="text", text=result)]

    elif name == "read_note":
        result = await run_obsidian(["read", f"file={arguments['file']}"])
        return [TextContent(type="text", text=result)]

    elif name == "create_note":
        args = ["create"]
        if "path" in arguments:
            args += [f"path={arguments['path']}"]
        else:
            args += [f"name={arguments['name']}"]
        args += [f"content={arguments['content']}"]
        if arguments.get("overwrite"):
            args += ["overwrite"]
        result = await run_obsidian(args)
        return [TextContent(type="text", text=f"Created: {result}")]

    elif name == "append_to_note":
        result = await run_obsidian(["append", f"file={arguments['file']}", f"content={arguments['content']}"])
        return [TextContent(type="text", text=f"Appended to note. {result}")]

    elif name == "get_backlinks":
        result = await run_obsidian(["backlinks", f"file={arguments['file']}", "format=json"])
        return [TextContent(type="text", text=result or "No backlinks found.")]

    elif name == "get_outgoing_links":
        result = await run_obsidian(["links", f"file={arguments['file']}"])
        return [TextContent(type="text", text=result or "No outgoing links found.")]

    elif name == "list_orphan_notes":
        result = await run_obsidian(["orphans"])
        return [TextContent(type="text", text=result or "No orphan notes found.")]

    elif name == "list_all_tasks":
        result = fs_tasks(
            arguments.get("filter", "todo"),
            arguments.get("file"),
        )
        return [TextContent(type="text", text=result)]

    elif name == "get_daily_note":
        result = fs_daily_read()
        return [TextContent(type="text", text=result)]

    elif name == "append_to_daily":
        result = fs_daily_append(arguments["content"])
        return [TextContent(type="text", text=f"Appended to daily note. {result}")]

    elif name == "list_vault_files":
        args = ["files"]
        if "folder" in arguments:
            args += [f"folder={arguments['folder']}"]
        result = await run_obsidian(args)
        return [TextContent(type="text", text=result)]

    elif name == "list_tags":
        result = await run_obsidian(["tags", "counts", "sort=count"])
        return [TextContent(type="text", text=result)]

    elif name == "get_vault_info":
        result = await run_obsidian(["vault"])
        return [TextContent(type="text", text=result)]

    elif name == "evening_reflection":
        r = arguments["responses"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        content = f"""

---
## 🌙 Evening Reflection — {now}

**What I accomplished today:**
{r.get('accomplished', 'Not filled in')}

**Where I wasted time / what slowed me down:**
{r.get('time_wasted', 'Not filled in')}

**Top priority for tomorrow:**
{r.get('tomorrow_priority', 'Not filled in')}

**Brain dump (anything on my mind):**
{r.get('brain_dump', 'Nothing to dump')}

**Energy level today:** {r.get('energy_level', 'not recorded')}
"""
        result = fs_daily_append(content)
        return [TextContent(type="text", text="Evening reflection saved to today's daily note.")]

    elif name == "update_goals":
        if arguments["action"] == "read":
            result = await run_obsidian(["read", "file=Goals"])
            return [TextContent(type="text", text=result or "Goals note not found. Create it first.")]
        else:
            result = await run_obsidian(["append", "file=Goals", f"content={arguments.get('content', '')}"])
            return [TextContent(type="text", text="Goals updated.")]

    elif name == "reading_queue":
        if arguments["action"] == "read":
            result = await run_obsidian(["read", "file=Reading Queue"])
            return [TextContent(type="text", text=result or "Reading Queue is empty.")]
        elif arguments["action"] == "add":
            item = arguments.get("item", "")
            now = datetime.now().strftime("%Y-%m-%d")
            content = f"\n- [ ] {item} — added {now}"
            result = await run_obsidian(["append", "file=Reading Queue", f"content={content}"])
            return [TextContent(type="text", text=f"Added to Reading Queue: {item}")]
        elif arguments["action"] == "mark_done":
            return [TextContent(type="text", text="To mark done: open Reading Queue and check the box, or ask me to search and update it.")]

    elif name == "recurring_tasks":
        if arguments["action"] == "read":
            result = await run_obsidian(["read", "file=Recurring Tasks"])
            return [TextContent(type="text", text=result or "Recurring Tasks note not found.")]
        elif arguments["action"] == "add":
            task = arguments.get("task", "")
            mins = arguments.get("estimated_minutes", 0)
            content = f"\n| {task} | {mins} min | |"
            result = await run_obsidian(["append", "file=Recurring Tasks", f"content={content}"])
            return [TextContent(type="text", text=f"Added task: {task} (~{mins} min)")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]

  except Exception as e:
    log(f"Error in tool '{name}': {e}")
    return [TextContent(type="text", text=f"Error in tool '{name}': {e}")]


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())