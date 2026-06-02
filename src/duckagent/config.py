import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ── Logging: only WARNING+ by default ────────────────────────────────
import structlog

_duck_log_level = os.environ.get("DUCKAGENT_LOG_LEVEL", "WARNING").upper()
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, _duck_log_level, logging.WARNING)
    ),
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(),
    ],
)

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    litellm_model: str = "anthropic/claude-sonnet-4-20250514"
    db_dir: str = ".duckagent"
    prompts_dir: str = "prompts"
    trace_code_file: str | None = None
    trace_rw_file: str | None = None
    trace_bl_file: str | None = None
    jadx_host: str = "127.0.0.1"
    jadx_port: int = 8650

    # Bus transport
    bus_transport: str = "local"  # "local" | "http"
    bus_server_host: str = "127.0.0.1"
    bus_server_port: int = 8720

    # ── MCP server configuration ──────────────────────────────────────

    # Comma-separated MCP server names each agent type connects to.
    # Server names are looked up in: built-in registry → DUCKAGENT_MCP_SERVERS
    # JSON → ~/.claude/.mcp.json (Claude Code format).
    trace_agent_mcp_servers: str = Field(default="trace", validation_alias="DUCKAGENT_TRACE_AGENT_MCP_SERVERS")
    ida_jadx_agent_mcp_servers: str = Field(default="ida-pro-mcp,jadx-mcp", validation_alias="DUCKAGENT_JADX_AGENT_MCP_SERVERS")
    main_agent_mcp_servers: str = Field(default="", validation_alias="DUCKAGENT_MAIN_AGENT_MCP_SERVERS")

    # JSON blob of custom MCP server configs.
    # Example: '{"ida":{"command":"python","args":["-m","my_ida_mcp"]}}'
    mcp_servers: str = ""

    # Paths to .mcp.json files (Claude Code compatible format).
    # Comma-separated, later files override earlier ones.
    mcp_json_paths: str = Field(default="~/.claude/.mcp.json", validation_alias="DUCKAGENT_MCP_JSON_PATHS")

    model_config = {"env_prefix": "DUCKAGENT_"}

    @property
    def db_path(self) -> Path:
        return Path(self.db_dir) / "messages.db"

    @property
    def prompts_path(self) -> Path:
        return Path(self.prompts_dir)

    @property
    def trace_files(self) -> dict[str, Path]:
        files = {}
        if self.trace_code_file:
            files["code"] = Path(self.trace_code_file)
        if self.trace_rw_file:
            files["rw"] = Path(self.trace_rw_file)
        if self.trace_bl_file:
            files["bl"] = Path(self.trace_bl_file)
        return files

    @property
    def bus_server_url(self) -> str:
        return f"http://{self.bus_server_host}:{self.bus_server_port}"

    @property
    def is_http_mode(self) -> bool:
        return self.bus_transport == "http"

    # ── MCP helpers ───────────────────────────────────────────────────

    def _get_agent_mcp_server_names(self, agent_type: str) -> list[str]:
        key = f"{agent_type}_mcp_servers"
        raw: str = getattr(self, key, "")
        if not raw.strip():
            return []
        return [n.strip() for n in raw.split(",") if n.strip()]

    def _build_mcp_registry(self) -> dict[str, Any]:
        """Build the full MCP server registry.

        Priority (low → high):
        1. Built-in servers (``BUILTIN_MCP_SERVERS``)
        2. ``DUCKAGENT_MCP_SERVERS`` JSON
        3. ``.mcp.json`` files (Claude Code compatible)
        """
        from duckagent.mcp.client_manager import BUILTIN_MCP_SERVERS, McpServerConfig, load_mcp_json

        registry: dict[str, Any] = dict(BUILTIN_MCP_SERVERS)

        # Merge DUCKAGENT_MCP_SERVERS JSON
        if self.mcp_servers.strip():
            try:
                extra: dict[str, dict[str, Any]] = json.loads(self.mcp_servers)
                for name, raw_cfg in extra.items():
                    raw_cfg["name"] = name
                    registry[name] = McpServerConfig.from_dict(raw_cfg)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                import structlog
                structlog.get_logger().warning("mcp_servers_parse_error", error=str(e))

        # Merge .mcp.json files (highest priority)
        if self.mcp_json_paths.strip():
            paths = [p.strip() for p in self.mcp_json_paths.split(",") if p.strip()]
            mcp_json_servers = load_mcp_json(*paths)
            registry.update(mcp_json_servers)

        return registry

    def resolve_mcp_configs(self, agent_type: str) -> list[Any]:
        """Build McpServerConfig list for an agent type.

        Returns configs for server names listed in ``{agent_type}_mcp_servers``.
        """
        registry = self._build_mcp_registry()
        names = self._get_agent_mcp_server_names(agent_type)

        configs: list[Any] = []
        for name in names:
            cfg = registry.get(name)
            if cfg is None:
                import structlog
                structlog.get_logger().warning(
                    "mcp_server_not_found", server=name, agent_type=agent_type)
                continue
            configs.append(self._fill_server_env(name, cfg))

        return configs

    def _fill_server_env(self, name: str, cfg: Any) -> Any:
        """Inject runtime env vars into a server config (returns a copy)."""
        from duckagent.mcp.client_manager import McpServerConfig

        env = dict(cfg.env)

        if name == "trace":
            existing = {k: v for k, v in self.trace_files.items() if v.exists()}
            env["DUCKAGENT_TRACE_CODE_FILE"] = str(existing.get("code", ""))
            env["DUCKAGENT_TRACE_RW_FILE"] = str(existing.get("rw", ""))
            env["DUCKAGENT_TRACE_BL_FILE"] = str(existing.get("bl", ""))

        if "jadx" in name:
            env.setdefault("DUCKAGENT_JADX_HOST", self.jadx_host)
            env.setdefault("DUCKAGENT_JADX_PORT", str(self.jadx_port))

        return McpServerConfig(
            name=cfg.name,
            command=cfg.command,
            args=list(cfg.args),
            env=env,
            url=cfg.url,
            headers=dict(cfg.headers),
            transport=cfg.transport,
            enabled=cfg.enabled,
        )


settings = Settings()
