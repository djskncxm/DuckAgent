"""MCP server exposing trace analysis tools via FastMCP (stdio transport).

Wraps ``LocalTraceToolExecutor`` behind the MCP protocol so any MCP client
(including DuckAgent itself) can call trace_search / trace_context /
trace_cross_ref without knowing about ak_search daemons.

Usage::

    python -m duckagent.mcp.servers.trace_server

Environment::

    DUCKAGENT_TRACE_CODE_FILE  path to code.log
    DUCKAGENT_TRACE_RW_FILE    path to rw.log
    DUCKAGENT_TRACE_BL_FILE    path to bl.log
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from duckagent.tools.trace_executor import LocalTraceToolExecutor

mcp = FastMCP("duckagent-trace")

_executor: LocalTraceToolExecutor | None = None


def get_executor() -> LocalTraceToolExecutor:
    """Lazy-init the trace executor from environment variables."""
    global _executor
    if _executor is not None:
        return _executor

    files: dict[str, Path] = {}
    for key, env_var in [
        ("code", "DUCKAGENT_TRACE_CODE_FILE"),
        ("rw", "DUCKAGENT_TRACE_RW_FILE"),
        ("bl", "DUCKAGENT_TRACE_BL_FILE"),
    ]:
        val = os.environ.get(env_var)
        if val:
            p = Path(val)
            if p.exists():
                files[key] = p

    if not files:
        raise FileNotFoundError(
            "No trace files found. Set DUCKAGENT_TRACE_CODE_FILE / "
            "DUCKAGENT_TRACE_RW_FILE / DUCKAGENT_TRACE_BL_FILE."
        )

    _executor = LocalTraceToolExecutor(files)
    return _executor


def _call_executor(tool_name: str, arguments: dict[str, Any]) -> str:
    """Route a tool call to the executor and return its JSON result string."""
    return get_executor().execute(tool_name, arguments)


# ── trace_search ─────────────────────────────────────────────────────


@mcp.tool()
def trace_search(
    query: Annotated[str, Field(description="Exact substring to find. Case-insensitive for ASCII.")],
    file: Annotated[
        Literal["code", "rw", "bl"],
        Field(description="Which trace file to search: 'code' = instruction log, 'rw' = memory hexdump log, 'bl' = external function call log."),
    ] = "code",
    from_line: Annotated[
        int | None,
        Field(description="1-based line to start searching from. Mutually exclusive with before_line."),
    ] = None,
    before_line: Annotated[
        int | None,
        Field(description="Search backward from this 1-based line. Mutually exclusive with from_line."),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Max matching lines to return. Must be <= 100.", ge=1, le=100),
    ] = 20,
) -> str:
    """Case-insensitive exact substring search over a session trace file.

    Use this to locate functions, registers, addresses, constants, and
    hexdump text in very large traces.  Every call must include exactly
    one of from_line or before_line, plus limit.  before_line searches
    backward and returns nearest earlier matches first.  For hex data
    starting with 0x, the harness automatically tries byte-reversed
    endian order as fallback.
    """
    args: dict[str, Any] = {"query": query, "file": file, "limit": limit}
    if from_line is not None:
        args["from_line"] = from_line
    if before_line is not None:
        args["before_line"] = before_line
    return _call_executor("trace_search", args)


# ── trace_context ────────────────────────────────────────────────────


@mcp.tool()
def trace_context(
    line: Annotated[int, Field(description="1-based target file line.")],
    file: Annotated[
        Literal["code", "rw", "bl"],
        Field(description="Which trace file to read context from."),
    ] = "code",
    before: Annotated[
        int,
        Field(description="Number of lines before the target. Must be <= 100.", ge=0, le=100),
    ] = 10,
    after: Annotated[
        int,
        Field(description="Number of lines after the target. Must be <= 100.", ge=0, le=100),
    ] = 10,
) -> str:
    """Return neighboring trace lines around a 1-based file line.

    Use after trace_search to inspect instruction context.  Each
    line-count argument must be no greater than 100.
    """
    return _call_executor("trace_context", {
        "line": line,
        "file": file,
        "before": before,
        "after": after,
    })


# ── trace_cross_ref ──────────────────────────────────────────────────


@mcp.tool()
def trace_cross_ref(
    seq_id: Annotated[str, Field(description="Hex sequence ID (without 0x prefix), e.g. '942' or 'a8dc9f'.")],
) -> str:
    """Look up all trace records correlated to a given hex sequence ID.

    Returns the code.log instruction line, any rw.log memory records,
    and any bl.log external function call records for that sequence.
    The sequence ID is the hex number at the start of lines in each
    trace file.
    """
    return _call_executor("trace_cross_ref", {"seq_id": seq_id})


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
