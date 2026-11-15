"""Middle-Agent Gateway.

An OpenAI-compatible chat completions endpoint that:

* routes every request through a tiered fallback chain via LiteLLM
  (free tiers first, then an ultra-cheap paid tier when quotas run out), and
* wraps each call with SQLite-backed graph memory hooks:
    - pre-hook : injects relevant context from earlier interactions of the same session,
    - post-hook: records the exchange as nodes + edges asynchronously after the response is sent.

Run:
    uvicorn gateway:app --host 0.0.0.0 --port 8000
    # or
    python gateway.py

Configuration is read from environment variables (and an optional ``.env`` file next to this file);
see ``.env.example`` for every supported key.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real environment variables always win."""
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


# Must run before importing litellm, which reads some of its settings at import time.
load_dotenv(BASE_DIR / ".env")

import litellm  # noqa: E402
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.encoders import jsonable_encoder  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

# ---------------------------------------------------------------------------- configuration
DEFAULT_MODEL_CHAIN = (
    "free:gemini/gemini-2.5-flash,"
    "free:openrouter/openrouter/free,"
    "paid:openai/gpt-6-luna"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60"))
MEMORY_ENABLED = _env_bool("MEMORY_ENABLED", True)
MEMORY_DB_PATH = BASE_DIR / os.getenv("MEMORY_DB_PATH", "agent_memory.db")
MEMORY_RECENT_LIMIT = int(os.getenv("MEMORY_RECENT_LIMIT", "6"))
MEMORY_RELATED_LIMIT = int(os.getenv("MEMORY_RELATED_LIMIT", "4"))
MEMORY_SNIPPET_CHARS = int(os.getenv("MEMORY_SNIPPET_CHARS", "500"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("middle-agent")

# Drop provider-unsupported params instead of failing, and keep LiteLLM's own logs quiet.
litellm.drop_params = True
litellm.suppress_debug_info = True

# Provider prefix -> API key env var. Tiers whose key is missing are skipped instead of failing.
PROVIDER_KEY_ENV: Dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

# Client-supplied params forwarded to the provider. Anything else (api_key, api_base, ...) is
# dropped so a caller can never redirect the gateway's credentials to another endpoint.
FORWARDED_PARAMS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "n",
    "presence_penalty", "frequency_penalty", "seed", "logit_bias", "logprobs", "top_logprobs",
    "tools", "tool_choice", "parallel_tool_calls", "response_format", "user", "reasoning_effort",
})

# Errors that mean "this tier cannot serve the request right now, try the next one".
FALLBACK_ERRORS: Tuple[type, ...] = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.BadRequestError,
    litellm.InternalServerError,
    litellm.APIError,
)


@dataclass(frozen=True)
class ModelTier:
    label: str
    model: str

    @property
    def provider(self) -> str:
        return self.model.split("/", 1)[0]

    def availability(self) -> Tuple[bool, str]:
        key_env = PROVIDER_KEY_ENV.get(self.provider)
        if key_env and not os.getenv(key_env):
            return False, f"{key_env} is not set"
        return True, "ready"


def parse_model_chain(raw: str) -> List[ModelTier]:
    """Parse "label:provider/model,..." entries; the label prefix (free|paid) is optional."""
    tiers: List[ModelTier] = []
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        label, separator, model = entry.partition(":")
        # Only treat the prefix as a label when it is one; model ids such as "...:free" contain colons.
        if separator and label in {"free", "paid"}:
            tiers.append(ModelTier(label=label, model=model.strip()))
        else:
            tiers.append(ModelTier(label="custom", model=entry))
    if not tiers:
        raise ValueError("MODEL_CHAIN is empty")
    return tiers


MODEL_CHAIN = parse_model_chain(os.getenv("MODEL_CHAIN", DEFAULT_MODEL_CHAIN))


# ---------------------------------------------------------------------------- graph memory
STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "what", "when", "where", "which", "about", "would",
    "could", "should", "there", "their", "them", "then", "than", "into", "your", "yours", "just",
    "bir", "için", "gibi", "daha", "olan", "nasıl", "neden", "bunu", "şunu", "veya", "ama", "ile",
})


def extract_keywords(text: str, limit: int = 5) -> List[str]:
    seen: List[str] = []
    for word in re.findall(r"\w{4,}", text.lower()):
        if word not in STOPWORDS and word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GraphMemory:
    """Lightweight graph store on SQLite.

    nodes: one row per message (user / assistant) scoped by session_id.
    edges: typed relations between nodes:
        follows      -> previous node in the session  ->  new user node
        responded_by -> user node                      ->  assistant answer
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS nodes (
        id          TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        metadata    TEXT NOT NULL DEFAULT '{}',
        created_at  REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_nodes_session_time ON nodes (session_id, created_at);

    CREATE TABLE IF NOT EXISTS edges (
        src_id      TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
        dst_id      TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
        relation    TEXT NOT NULL,
        created_at  REAL NOT NULL,
        PRIMARY KEY (src_id, dst_id, relation)
    );
    CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_id);
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- schema -----------------------------------------------------------------
    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            # WAL lets the background writer and request readers work concurrently.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(self.SCHEMA)
            connection.commit()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    # -- read (pre-hook) ----------------------------------------------------------
    def _context_sync(self, session_id: str, query: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            recent = connection.execute(
                "SELECT id, role, content, created_at FROM nodes "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, MEMORY_RECENT_LIMIT),
            ).fetchall()
            selected: Dict[str, sqlite3.Row] = {row["id"]: row for row in recent}

            keywords = extract_keywords(query)
            if keywords and MEMORY_RELATED_LIMIT > 0:
                like_clause = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in keywords)
                related = connection.execute(
                    f"SELECT id, role, content, created_at FROM nodes "
                    f"WHERE session_id = ? AND ({like_clause}) "
                    f"ORDER BY created_at DESC LIMIT ?",
                    (session_id, *[f"%{escape_like(word)}%" for word in keywords], MEMORY_RELATED_LIMIT),
                ).fetchall()
                new_related = [row for row in related if row["id"] not in selected]
                selected.update({row["id"]: row for row in new_related})

                # One-hop graph expansion: pull the other side of every responded_by edge so a
                # matched question brings its answer along (and vice versa).
                if new_related:
                    ids = [row["id"] for row in new_related]
                    placeholders = ",".join("?" for _ in ids)
                    neighbours = connection.execute(
                        f"SELECT n.id, n.role, n.content, n.created_at FROM edges e "
                        f"JOIN nodes n ON n.id = CASE WHEN e.src_id IN ({placeholders}) THEN e.dst_id ELSE e.src_id END "
                        f"WHERE e.relation = 'responded_by' "
                        f"AND (e.src_id IN ({placeholders}) OR e.dst_id IN ({placeholders}))",
                        (*ids, *ids, *ids),
                    ).fetchall()
                    selected.update({row["id"]: row for row in neighbours})

        rows = sorted(selected.values(), key=lambda row: row["created_at"])
        return [dict(row) for row in rows]

    async def fetch_context(self, session_id: str, query: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._context_sync, session_id, query)

    # -- write (post-hook) --------------------------------------------------------
    def _record_sync(self, session_id: str, user_text: str, assistant_text: str, metadata: Dict[str, Any]) -> None:
        now = time.time()
        user_id, assistant_id = uuid.uuid4().hex, uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT id FROM nodes WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            connection.executemany(
                "INSERT INTO nodes (id, session_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (user_id, session_id, "user", user_text, "{}", now),
                    (assistant_id, session_id, "assistant", assistant_text, json.dumps(metadata), now + 1e-6),
                ],
            )
            edges = [(user_id, assistant_id, "responded_by", now)]
            if previous is not None:
                edges.append((previous["id"], user_id, "follows", now))
            connection.executemany(
                "INSERT OR IGNORE INTO edges (src_id, dst_id, relation, created_at) VALUES (?, ?, ?, ?)",
                edges,
            )

    async def record_exchange(self, session_id: str, user_text: str, assistant_text: str, metadata: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._record_sync, session_id, user_text, assistant_text, metadata)

    # -- stats --------------------------------------------------------------------
    def _stats_sync(self) -> Dict[str, int]:
        with closing(self._connect()) as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            sessions = connection.execute("SELECT COUNT(DISTINCT session_id) FROM nodes").fetchone()[0]
        return {"nodes": nodes, "edges": edges, "sessions": sessions}

    async def stats(self) -> Dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)


memory = GraphMemory(MEMORY_DB_PATH)


# ---------------------------------------------------------------------------- hooks
def message_text(content: Any) -> str:
    """Flatten OpenAI message content (string or multimodal parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return str(content)


def last_user_text(messages: Sequence[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message_text(message.get("content"))
    return ""


async def pre_hook(session_id: str, messages: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Inject remembered context as a system message placed after the client's own system prompts."""
    try:
        context_rows = await memory.fetch_context(session_id, query)
    except sqlite3.Error:
        logger.exception("pre-hook: memory lookup failed, continuing without context")
        return messages
    if not context_rows:
        return messages

    lines = []
    for row in context_rows:
        snippet = " ".join(row["content"].split())
        if len(snippet) > MEMORY_SNIPPET_CHARS:
            snippet = snippet[:MEMORY_SNIPPET_CHARS] + "..."
        lines.append(f"- [{row['role']}] {snippet}")
    context_message = {
        "role": "system",
        "content": "Relevant memory from earlier interactions in this session "
                   "(may be outdated, use only if helpful):\n" + "\n".join(lines),
    }

    insert_at = 0
    while insert_at < len(messages) and messages[insert_at].get("role") == "system":
        insert_at += 1
    logger.debug("pre-hook: injected %d memory nodes for session %s", len(context_rows), session_id)
    return [*messages[:insert_at], context_message, *messages[insert_at:]]


async def post_hook(session_id: str, user_text: str, assistant_text: str, metadata: Dict[str, Any]) -> None:
    """Runs as a background task after the response has been sent; must never raise."""
    try:
        await memory.record_exchange(session_id, user_text, assistant_text, metadata)
    except Exception:  # noqa: BLE001 - background task, log and swallow
        logger.exception("post-hook: failed to record exchange for session %s", session_id)


# ---------------------------------------------------------------------------- fallback chain
class AllTiersFailedError(Exception):
    def __init__(self, attempts: List[Dict[str, Any]]) -> None:
        super().__init__("All model tiers failed")
        self.attempts = attempts


async def complete_with_fallback(
    messages: List[Dict[str, Any]], params: Dict[str, Any]
) -> Tuple[Any, ModelTier, List[Dict[str, Any]]]:
    attempts: List[Dict[str, Any]] = []
    for tier in MODEL_CHAIN:
        available, reason = tier.availability()
        if not available:
            attempts.append({"model": tier.model, "tier": tier.label, "status": "skipped", "reason": reason})
            continue

        started = time.perf_counter()
        try:
            # num_retries=0: the chain itself is the retry strategy, waiting on a rate-limited tier wastes time.
            response = await litellm.acompletion(
                model=tier.model,
                messages=messages,
                timeout=REQUEST_TIMEOUT,
                num_retries=0,
                **params,
            )
        except FALLBACK_ERRORS as exc:
            logger.warning("tier %s (%s) failed: %s", tier.label, tier.model, type(exc).__name__)
            attempts.append(_failed_attempt(tier, exc, started))
        except Exception as exc:  # noqa: BLE001 - unknown provider errors must not stop the chain
            logger.exception("tier %s (%s) raised an unexpected error", tier.label, tier.model)
            attempts.append(_failed_attempt(tier, exc, started))
        else:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            attempts.append({"model": tier.model, "tier": tier.label, "status": "ok", "latency_ms": elapsed_ms})
            logger.info("served by %s tier %s in %d ms", tier.label, tier.model, elapsed_ms)
            return response, tier, attempts

    raise AllTiersFailedError(attempts)


def _failed_attempt(tier: ModelTier, exc: Exception, started: float) -> Dict[str, Any]:
    return {
        "model": tier.model,
        "tier": tier.label,
        "status": "failed",
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


# ---------------------------------------------------------------------------- API schema
class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = Field(default=None, description="Ignored: the gateway always applies MODEL_CHAIN.")
    messages: List[ChatMessage] = Field(min_length=1)
    stream: bool = False


async def require_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    if not GATEWAY_API_KEY:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.strip().encode("utf-8"), GATEWAY_API_KEY.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})


def normalise_session_id(raw: Optional[str]) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:@-]", "", (raw or "").strip())[:128]
    return cleaned or "default"


# ---------------------------------------------------------------------------- application
@asynccontextmanager
async def lifespan(_: FastAPI):
    if MEMORY_ENABLED:
        await memory.initialize()
        logger.info("graph memory ready at %s", MEMORY_DB_PATH)
    for index, tier in enumerate(MODEL_CHAIN, start=1):
        available, reason = tier.availability()
        logger.info("tier %d [%s] %s -> %s", index, tier.label, tier.model, reason if not available else "ready")
    if not GATEWAY_API_KEY:
        logger.warning("GATEWAY_API_KEY is empty: the API is unauthenticated, bind to 127.0.0.1 or set a key")
    yield


app = FastAPI(title="Middle-Agent Gateway", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health() -> Dict[str, Any]:
    chain = []
    for tier in MODEL_CHAIN:
        available, reason = tier.availability()
        chain.append({"model": tier.model, "tier": tier.label, "available": available, "reason": reason})
    payload: Dict[str, Any] = {"status": "ok", "memory_enabled": MEMORY_ENABLED, "chain": chain}
    if MEMORY_ENABLED:
        try:
            payload["memory"] = await memory.stats()
        except sqlite3.Error as exc:
            payload["status"] = "degraded"
            payload["memory_error"] = str(exc)
    return payload


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models() -> Dict[str, Any]:
    created = int(time.time())
    data = [{"id": "middle-agent", "object": "model", "created": created, "owned_by": "middle-agent"}]
    for tier in MODEL_CHAIN:
        available, _ = tier.availability()
        data.append({
            "id": tier.model,
            "object": "model",
            "created": created,
            "owned_by": tier.provider,
            "tier": tier.label,
            "available": available,
        })
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(
    body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    x_session_id: Optional[str] = Header(default=None),
    x_memory: Optional[str] = Header(default=None),
) -> JSONResponse:
    if body.stream:
        raise HTTPException(status_code=400, detail="stream=true is not supported yet, send stream=false")

    session_id = normalise_session_id(x_session_id)
    use_memory = MEMORY_ENABLED and (x_memory or "").strip().lower() != "off"
    messages = [message.model_dump(exclude_none=True) for message in body.messages]
    user_text = last_user_text(messages)

    if use_memory and user_text:
        messages = await pre_hook(session_id, messages, user_text)

    params = {key: value for key, value in (body.model_extra or {}).items() if key in FORWARDED_PARAMS}

    try:
        response, tier, attempts = await complete_with_fallback(messages, params)
    except AllTiersFailedError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "All model tiers failed or are unavailable",
                    "type": "all_tiers_failed",
                    "attempts": exc.attempts,
                }
            },
        )

    payload = jsonable_encoder(response.model_dump())
    choices = payload.get("choices") or [{}]
    assistant_text = message_text((choices[0].get("message") or {}).get("content"))

    if use_memory and user_text and assistant_text:
        background_tasks.add_task(
            post_hook,
            session_id,
            user_text,
            assistant_text,
            {"model": tier.model, "tier": tier.label, "usage": payload.get("usage")},
        )

    headers = {
        "X-Gateway-Model": tier.model,
        "X-Gateway-Tier": tier.label,
        "X-Gateway-Session": session_id,
        "X-Gateway-Attempts": str(len(attempts)),
    }
    return JSONResponse(content=payload, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "gateway:app",
        host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("GATEWAY_PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
