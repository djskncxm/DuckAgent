"""MCP server exposing JADX static analysis tools via FastMCP (stdio transport).

Wraps ``JadxToolExecutor`` behind the MCP protocol so any MCP client
(including DuckAgent itself) can call the 11 JADX tools without knowing
about the underlying HTTP API.

Usage::

    python -m duckagent.mcp.servers.jadx_server

Environment::

    DUCKAGENT_JADX_HOST   JADX GUI host (default 127.0.0.1)
    DUCKAGENT_JADX_PORT   JADX AI MCP Plugin port (default 8650)
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from duckagent.tools.jadx_executor import JadxToolExecutor

mcp = FastMCP("duckagent-jadx")

_executor: JadxToolExecutor | None = None


def get_executor() -> JadxToolExecutor:
    """Lazy-init the JADX executor from environment variables."""
    global _executor
    if _executor is not None:
        return _executor

    host = os.environ.get("DUCKAGENT_JADX_HOST", "127.0.0.1")
    port = int(os.environ.get("DUCKAGENT_JADX_PORT", "8650"))
    _executor = JadxToolExecutor(jadx_host=host, jadx_port=port)
    return _executor


def _call_executor(tool_name: str, arguments: dict[str, Any]) -> str:
    return get_executor().execute(tool_name, arguments)


# ── jadx_search_classes_by_keyword ────────────────────────────────────


@mcp.tool()
def jadx_search_classes_by_keyword(
    search_term: Annotated[str, Field(description="The keyword or string to search for.")],
    package: Annotated[str | None, Field(description="Optional package name to limit search scope.")] = None,
    search_in: Annotated[
        str | None,
        Field(description="Search scope: class names, method names, fields, code bodies, or comments."),
    ] = "code",
    offset: Annotated[int | None, Field(description="Pagination offset.", ge=0)] = 0,
    count: Annotated[int | None, Field(description="Max results (1-50).", ge=1, le=50)] = 20,
) -> str:
    """Search decompiled APK code for classes containing a keyword.

    Can search in class names, method names, fields, code bodies, or
    comments.  Use this as your primary discovery tool to find relevant
    code.
    """
    return _call_executor("jadx_search_classes_by_keyword", {
        "search_term": search_term,
        "package": package or "",
        "search_in": search_in or "code",
        "offset": offset or 0,
        "count": count or 20,
    })


# ── jadx_get_class_source ──────────────────────────────────────────────


@mcp.tool()
def jadx_get_class_source(
    class_name: Annotated[str, Field(description="Full qualified class name, e.g. 'com.example.app.MainActivity'.")],
) -> str:
    """Fetch the decompiled Java source code of a specific class by its full qualified name."""
    return _call_executor("jadx_get_class_source", {"class_name": class_name})


# ── jadx_get_method_by_name ────────────────────────────────────────────


@mcp.tool()
def jadx_get_method_by_name(
    class_name: Annotated[str, Field(description="Full qualified class name.")],
    method_name: Annotated[str, Field(description="Method name to fetch (without parameter types).")],
) -> str:
    """Fetch the source code of a specific method from a class."""
    return _call_executor("jadx_get_method_by_name", {
        "class_name": class_name,
        "method_name": method_name,
    })


# ── jadx_get_xrefs_to_class ────────────────────────────────────────────


@mcp.tool()
def jadx_get_xrefs_to_class(
    class_name: Annotated[str, Field(description="Full qualified class name to find references to.")],
    offset: Annotated[int | None, Field(description="Pagination offset.", ge=0)] = 0,
    count: Annotated[int | None, Field(description="Max results (1-50).", ge=1, le=50)] = 20,
) -> str:
    """Find all references to a class throughout the APK codebase."""
    return _call_executor("jadx_get_xrefs_to_class", {
        "class_name": class_name,
        "offset": offset or 0,
        "count": count or 20,
    })


# ── jadx_get_xrefs_to_method ───────────────────────────────────────────


@mcp.tool()
def jadx_get_xrefs_to_method(
    class_name: Annotated[str, Field(description="Full qualified class name containing the method.")],
    method_name: Annotated[str, Field(description="Method name to find call sites for.")],
    offset: Annotated[int | None, Field(description="Pagination offset.", ge=0)] = 0,
    count: Annotated[int | None, Field(description="Max results (1-50).", ge=1, le=50)] = 20,
) -> str:
    """Find all call sites of a specific method throughout the APK codebase."""
    return _call_executor("jadx_get_xrefs_to_method", {
        "class_name": class_name,
        "method_name": method_name,
        "offset": offset or 0,
        "count": count or 20,
    })


# ── jadx_get_methods_of_class ──────────────────────────────────────────


@mcp.tool()
def jadx_get_methods_of_class(
    class_name: Annotated[str, Field(description="Full qualified class name.")],
) -> str:
    """List all method names in a class."""
    return _call_executor("jadx_get_methods_of_class", {"class_name": class_name})


# ── jadx_get_fields_of_class ───────────────────────────────────────────


@mcp.tool()
def jadx_get_fields_of_class(
    class_name: Annotated[str, Field(description="Full qualified class name.")],
) -> str:
    """List all field names in a class."""
    return _call_executor("jadx_get_fields_of_class", {"class_name": class_name})


# ── jadx_get_android_manifest ──────────────────────────────────────────


@mcp.tool()
def jadx_get_android_manifest() -> str:
    """Retrieve and return the AndroidManifest.xml content."""
    return _call_executor("jadx_get_android_manifest", {})


# ── jadx_get_smali_of_class ────────────────────────────────────────────


@mcp.tool()
def jadx_get_smali_of_class(
    class_name: Annotated[str, Field(description="Full qualified class name.")],
) -> str:
    """Fetch the smali bytecode representation of a class (useful for deep analysis)."""
    return _call_executor("jadx_get_smali_of_class", {"class_name": class_name})


# ── jadx_get_strings ───────────────────────────────────────────────────


@mcp.tool()
def jadx_get_strings(
    offset: Annotated[int | None, Field(description="Pagination offset.", ge=0)] = 0,
    count: Annotated[int | None, Field(description="Max results (0 = all).", ge=0)] = 0,
) -> str:
    """Retrieve contents of strings.xml resource files."""
    return _call_executor("jadx_get_strings", {
        "offset": offset or 0,
        "count": count or 0,
    })


# ── jadx_get_main_activity_class ───────────────────────────────────────


@mcp.tool()
def jadx_get_main_activity_class() -> str:
    """Fetch the main activity class from AndroidManifest.xml."""
    return _call_executor("jadx_get_main_activity_class", {})


# ── Entry point ────────────────────────────────────────────────────────


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
