"""Node configuration.

Structure lives in ``config.yaml`` (node roles, model chain, peers); secrets and per-host
overrides come from environment variables or a ``.env`` file. Environment always wins.

Everything magent keeps on this machine lives in one home directory: ``$MAGENT_HOME`` or
``~/.config/middle-agentic`` (config.yaml, .env, agent_memory.db). The installed package never reads
or writes the source checkout, so the repository can be moved or deleted after installation.
The current working directory is deliberately not searched either.
"""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

def magent_home() -> Path:
    explicit = os.getenv("MAGENT_HOME")
    return Path(explicit).expanduser() if explicit else Path.home() / ".config" / "middle-agentic"


USER_CONFIG_PATH = magent_home() / "config.yaml"

ROLES = ("gateway", "llm", "client")
TierLabel = Literal["free", "local", "paid", "custom"]


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); variables already in the environment win."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_environment() -> None:
    load_dotenv(magent_home() / ".env")


def _default_node_name() -> str:
    return socket.gethostname().split(".")[0].lower() or "node"


class NodeConfig(BaseModel):
    name: str = Field(default_factory=_default_node_name)
    roles: list[str] = Field(default_factory=lambda: list(ROLES))

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, roles: list[str]) -> list[str]:
        unknown = sorted(set(roles) - set(ROLES))
        if unknown:
            raise ValueError(f"unknown roles {unknown}, allowed: {list(ROLES)}")
        return roles


class GatewayConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    request_timeout: float = 60.0


class MemoryConfig(BaseModel):
    enabled: bool = True
    db_path: str = "agent_memory.db"
    recent_limit: int = 6
    related_limit: int = 4
    important_limit: int = 5
    snippet_chars: int = 500

    def resolved_db_path(self) -> Path:
        path = Path(self.db_path).expanduser()
        return path if path.is_absolute() else magent_home() / path


class LLMConfig(BaseModel):
    """The node's own local model server (role: llm)."""

    runtime: str = "ollama"
    model: str = "qwen2.5:3b"
    api_base: str | None = None


class TierConfig(BaseModel):
    name: str
    tier: TierLabel = "custom"
    runtime: str
    model: str
    api_base: str | None = None
    api_key_env: str | None = None
    timeout: float | None = None
    # USD per 1M tokens. When set they override LiteLLM's price table, which is how brand-new models get priced.
    price_in: float | None = Field(default=None, ge=0)
    price_out: float | None = Field(default=None, ge=0)
    # Requests in flight at once for this tier (None = local tiers 1, cloud tiers unlimited).
    max_concurrency: int | None = Field(default=None, ge=0)
    # Seconds a request waits for a free slot before the chain moves on to the next tier.
    queue_wait: float = Field(default=30.0, ge=0)
    # Attempts per day (local day, or in reset_tz, an IANA name such as America/Los_Angeles). None = no cap.
    daily_request_cap: int | None = Field(default=None, ge=0)
    reset_tz: str | None = None
    # After a rate-limit error the tier is skipped for this many seconds.
    cooldown_seconds: float = Field(default=900.0, ge=0)
    # Capabilities. None = unknown, so the tier is not excluded.
    supports_tools: bool | None = None
    context_window: int | None = Field(default=None, ge=0)


class BudgetConfig(BaseModel):
    """Daily spend cap across all keys. 0 disables the cap.

    downgrade: once the day's spend reaches the cap, paid tiers are skipped; free and local tiers still answer.
    block: once the cap is reached, requests get HTTP 429 until the local day changes.
    """

    daily_usd: float = Field(default=1.0, ge=0)
    on_exceed: Literal["downgrade", "block"] = "downgrade"


class PeerConfig(BaseModel):
    name: str
    url: str
    api_key_env: str | None = None


class DiscoveryConfig(BaseModel):
    tailscale: bool = True
    port: int = 8000
    probe_timeout: float = 1.5


class ClientConfig(BaseModel):
    default_node: str | None = None
    timeout: float = 120.0


class Settings(BaseModel):
    node: NodeConfig = Field(default_factory=NodeConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    chain: list[TierConfig] = Field(default_factory=list)
    peers: list[PeerConfig] = Field(default_factory=list)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    client: ClientConfig = Field(default_factory=ClientConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="after")
    def _unique_tier_names(self) -> "Settings":
        names = [tier.name for tier in self.chain]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate chain tier names: {duplicates}")
        return self

    def has_role(self, role: str) -> bool:
        return role in self.node.roles


def default_chain(llm: LLMConfig) -> list[TierConfig]:
    """Cloud first, local model as the free safety net, ultra-cheap paid model last."""
    return [
        TierConfig(name="gemini-flash", tier="free", runtime="gemini", model="gemini-2.5-flash"),
        TierConfig(name="openrouter-free", tier="free", runtime="openrouter", model="openrouter/free"),
        TierConfig(name="local", tier="local", runtime=llm.runtime, model=llm.model, api_base=llm.api_base),
        TierConfig(name="luna", tier="paid", runtime="openai", model="gpt-6-luna"),
    ]


_LEGACY_RUNTIME_ALIASES = {"ollama": "ollama", "ollama_chat": "ollama"}


def parse_legacy_chain(raw: str) -> list[TierConfig]:
    """Parse the v1 ``MODEL_CHAIN`` format: ``[free:|paid:|local:]provider/model,...``."""
    from middle_agentic.providers.registry import RUNTIMES

    tiers: list[TierConfig] = []
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        label, separator, rest = entry.partition(":")
        # Model ids such as "...:free" contain colons, so only known labels count as a prefix.
        if separator and label in ("free", "paid", "local"):
            tier_label, model_id = label, rest.strip()
        else:
            tier_label, model_id = "custom", entry
        provider, _, model = model_id.partition("/")
        runtime = _LEGACY_RUNTIME_ALIASES.get(provider, provider)
        if model and runtime in RUNTIMES:
            tiers.append(TierConfig(name=model_id, tier=tier_label, runtime=runtime, model=model))
        else:
            tiers.append(TierConfig(name=model_id, tier=tier_label, runtime="litellm", model=model_id))
    if not tiers:
        raise ValueError("MODEL_CHAIN is empty")
    return tiers


def find_config_file() -> Path | None:
    explicit = os.getenv("MAGENT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (magent_home() / "config.yaml",):
        if candidate.is_file():
            return candidate
    return None


def _env_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _apply_env_overrides(data: dict[str, Any]) -> None:
    overrides: list[tuple[str, tuple[str, str], Any]] = [
        ("GATEWAY_HOST", ("gateway", "host"), str),
        ("GATEWAY_PORT", ("gateway", "port"), int),
        ("REQUEST_TIMEOUT", ("gateway", "request_timeout"), float),
        ("MEMORY_ENABLED", ("memory", "enabled"), _env_bool),
        ("MEMORY_DB_PATH", ("memory", "db_path"), str),
        ("MEMORY_RECENT_LIMIT", ("memory", "recent_limit"), int),
        ("MEMORY_RELATED_LIMIT", ("memory", "related_limit"), int),
        ("MEMORY_IMPORTANT_LIMIT", ("memory", "important_limit"), int),
        ("MEMORY_SNIPPET_CHARS", ("memory", "snippet_chars"), int),
        ("MAGENT_NODE_NAME", ("node", "name"), str),
        ("LLM_RUNTIME", ("llm", "runtime"), str),
        ("LLM_MODEL", ("llm", "model"), str),
        ("LLM_API_BASE", ("llm", "api_base"), str),
        ("MAGENT_DEFAULT_NODE", ("client", "default_node"), str),
    ]
    for env_name, (section, key), cast in overrides:
        value = os.getenv(env_name)
        if value is None or value == "":
            continue
        data.setdefault(section, {})[key] = cast(value)

    roles = os.getenv("MAGENT_ROLES")
    if roles:
        data.setdefault("node", {})["roles"] = [role.strip() for role in roles.split(",") if role.strip()]


def load_settings(path: Path | None = None) -> Settings:
    load_environment()
    config_path = path or find_config_file()
    data: dict[str, Any] = {}
    if config_path and config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{config_path} must contain a YAML mapping")

    _apply_env_overrides(data)
    legacy_chain = os.getenv("MODEL_CHAIN")
    if legacy_chain:
        data["chain"] = [tier.model_dump() for tier in parse_legacy_chain(legacy_chain)]

    settings = Settings.model_validate(data)
    if not settings.chain:
        settings.chain = default_chain(settings.llm)
    return settings


def client_api_key() -> str:
    """Key this machine presents to gateways: MAGENT_API_KEY if set, else the Keychain/DPAPI item `gateway`.

    The Keychain is read only when no environment value exists, and any failure there (no store on
    this platform, item missing) yields an empty string, so callers get a plain 401 instead of a crash.
    """
    load_environment()
    value = os.getenv("MAGENT_API_KEY", "").strip()
    if value:
        return value
    try:
        from middle_agentic import tokens

        return tokens.get_token(tokens.default_store(), "gateway")
    except Exception:  # noqa: BLE001 - no key is a normal state; the request then fails with 401
        return ""
