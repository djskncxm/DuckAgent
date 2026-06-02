"""MCP server exposing general-purpose file tools via FastMCP (stdio transport).

All agents connect to this server to read/write any files on the system —
analysis documents, source code, binaries, configs, etc.

Relative paths are resolved against DUCKAGENT_FILE_BASE_DIR (defaults to CWD).

Usage::

    python -m duckagent.mcp.servers.file_server

Environment::

    DUCKAGENT_FILE_BASE_DIR  base directory for resolving relative paths
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("duckagent-file")


def _base_dir() -> Path:
    raw = os.environ.get("DUCKAGENT_FILE_BASE_DIR", "")
    return Path(raw).resolve() if raw else Path.cwd().resolve()


def _resolve(path: str) -> Path:
    """Resolve path: absolute stays absolute, relative resolves against base dir."""
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (_base_dir() / p).resolve()


@mcp.tool()
def file_write(
    path: Annotated[str, Field(description="File path (absolute or relative to project root)")],
    content: Annotated[str, Field(description="Full file content to write")],
) -> str:
    """Write (create or overwrite) a file. Parent directories are created automatically."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps({"status": "ok", "path": str(target), "bytes": len(content.encode("utf-8"))})


@mcp.tool()
def file_read(
    path: Annotated[str, Field(description="File path (absolute or relative to project root)")],
    offset: Annotated[int, Field(description="0-based line offset to start reading from")] = 0,
    limit: Annotated[int, Field(description="Max lines to return, 0 = all", ge=0)] = 0,
) -> str:
    """Read a file. Supports line-based pagination via offset/limit for large files."""
    target = _resolve(path)
    if not target.exists():
        return json.dumps({"status": "error", "error": f"File not found: {target}"})
    if not target.is_file():
        return json.dumps({"status": "error", "error": f"Not a file: {target}"})

    text = target.read_text(encoding="utf-8")
    if offset > 0 or limit > 0:
        lines = text.splitlines(keepends=True)
        if offset > 0:
            lines = lines[offset:]
        if limit > 0:
            lines = lines[:limit]
        text = "".join(lines)

    return text


@mcp.tool()
def file_list(
    path: Annotated[str, Field(description="Directory path (absolute or relative), empty for project root")] = "",
    recursive: Annotated[bool, Field(description="List recursively")] = False,
) -> str:
    """List files and directories. Returns JSON array with path, type, and size."""
    target = _resolve(path) if path else _base_dir()
    if not target.exists():
        return json.dumps({"status": "error", "error": f"Directory not found: {target}"})
    if not target.is_dir():
        return json.dumps({"status": "error", "error": f"Not a directory: {target}"})

    entries = []
    items = sorted(target.rglob("*")) if recursive else sorted(target.iterdir())
    for item in items:
        try:
            entries.append({
                "path": str(item),
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })
        except OSError:
            continue

    return json.dumps(entries, ensure_ascii=False)


@mcp.tool()
def file_append(
    path: Annotated[str, Field(description="File path (absolute or relative to project root)")],
    content: Annotated[str, Field(description="Content to append")],
) -> str:
    """Append content to a file, or create it if it doesn't exist."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(content)
    size = target.stat().st_size
    return json.dumps({"status": "ok", "path": str(target), "bytes_total": size})


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
