"""Python client for any middle-agentic gateway (local or on another tailnet device).

    from middle_agentic.client import MiddleAgentClient

    with MiddleAgentClient(node="acer-worker", session_id="research") as client:
        print(client.ask("Summarise RFC 9110 in 3 bullets", model="local").text)
        for delta in client.stream("Write a haiku about fallbacks"):
            print(delta, end="", flush=True)
        client.record_event("Decided to use Postgres", role="decision")
        print(client.context("database decision")["context"])
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from middle_agentic.config import Settings, client_api_key, load_settings
from middle_agentic.client.discovery import Node, discover_nodes, resolve_node


class GatewayError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(f"gateway returned HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass
class ChatResult:
    text: str
    raw: dict[str, Any]
    node: str = ""
    tier_name: str = ""
    tier: str = ""
    model: str = ""
    session_id: str = ""
    attempts: int = 0
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def _messages(prompt_or_messages: str | list[dict[str, Any]], system: str | None) -> list[dict[str, Any]]:
    if isinstance(prompt_or_messages, str):
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt_or_messages}]
    else:
        messages = list(prompt_or_messages)
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


class MiddleAgentClient:
    def __init__(
        self,
        node: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        if base_url:
            self.node = Node(name=base_url, url=base_url.rstrip("/"), source="url", api_key=client_api_key())
        else:
            self.node = resolve_node(node, self.settings)
        key = api_key if api_key is not None else self.node.api_key
        self.session_id = session_id
        self.last_headers: dict[str, str] = {}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._http = httpx.Client(
            base_url=self.node.url,
            headers=headers,
            timeout=timeout or self.settings.client.timeout,
        )

    @property
    def base_url(self) -> str:
        return self.node.url

    # -- lifecycle ---------------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MiddleAgentClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------------------
    def _session(self, session_id: str | None) -> str:
        return session_id or self.session_id or "default"

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise GatewayError(response.status_code, _error_detail(response))
        return response

    # -- discovery / status ------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def models(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/models").json()["data"]

    # -- admin (read-only; needs a key with the admin scope) ---------------------------
    def admin_models(self) -> list[dict[str, Any]]:
        return self._request("GET", "/admin/models").json()["data"]

    def usage(self, by: str = "tier", days: int = 1) -> list[dict[str, Any]]:
        return self._request("GET", "/admin/usage", params={"by": by, "days": days}).json()["data"]

    def budget(self) -> dict[str, Any]:
        return self._request("GET", "/admin/budget").json()

    # -- admin: live changes (take effect on the next request) ---------------------------
    @staticmethod
    def _given(**fields: Any) -> dict[str, Any]:
        return {key: value for key, value in fields.items() if value is not None}

    def patch_model(self, name: str, **changes: Any) -> dict[str, Any]:
        return self._request("PATCH", f"/admin/models/{name}", json=self._given(**changes)).json()

    def reset_model(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/admin/models/{name}").json()

    def set_model_order(self, names: list[str]) -> dict[str, Any]:
        return self._request("PUT", "/admin/models/order", json={"names": names}).json()

    def clear_model_order(self) -> dict[str, Any]:
        return self._request("DELETE", "/admin/models/order").json()

    def clear_overrides(self) -> dict[str, Any]:
        return self._request("DELETE", "/admin/overrides").json()

    def set_budget(self, daily_usd: float | None = None, on_exceed: str | None = None) -> dict[str, Any]:
        return self._request("PUT", "/admin/budget", json=self._given(daily_usd=daily_usd, on_exceed=on_exceed)).json()

    def clear_budget(self) -> dict[str, Any]:
        return self._request("DELETE", "/admin/budget").json()

    @staticmethod
    def discover(settings: Settings | None = None, only_online: bool = True) -> list[Node]:
        return discover_nodes(settings or load_settings(), only_online=only_online)

    # -- prompts -----------------------------------------------------------------------
    def chat(
        self,
        messages: str | list[dict[str, Any]],
        *,
        model: str = "auto",
        system: str | None = None,
        session_id: str | None = None,
        memory: bool = True,
        job: str | None = None,
        budget_usd: float | None = None,
        **params: Any,
    ) -> ChatResult:
        session = self._session(session_id)
        response = self._request(
            "POST",
            "/v1/chat/completions",
            json={"model": model, "messages": _messages(messages, system), "stream": False, **params},
            headers=_gateway_headers(session, memory, job, budget_usd),
        )
        payload = response.json()
        self.last_headers = dict(response.headers)
        choices = payload.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        return ChatResult(
            text=text,
            raw=payload,
            node=response.headers.get("x-gateway-node", ""),
            tier_name=response.headers.get("x-gateway-tier-name", ""),
            tier=response.headers.get("x-gateway-tier", ""),
            model=response.headers.get("x-gateway-model", ""),
            session_id=response.headers.get("x-gateway-session", session),
            attempts=int(response.headers.get("x-gateway-attempts", "0") or 0),
            headers=dict(response.headers),
        )

    def ask(self, prompt: str, **kwargs: Any) -> ChatResult:
        return self.chat(prompt, **kwargs)

    def stream(
        self,
        messages: str | list[dict[str, Any]],
        *,
        model: str = "auto",
        system: str | None = None,
        session_id: str | None = None,
        memory: bool = True,
        job: str | None = None,
        budget_usd: float | None = None,
        **params: Any,
    ) -> Iterator[str]:
        """Yield text deltas. Gateway headers (tier, model) are in ``last_headers`` once iteration starts."""
        session = self._session(session_id)
        with self._http.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": model, "messages": _messages(messages, system), "stream": True, **params},
            headers=_gateway_headers(session, memory, job, budget_usd),
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise GatewayError(response.status_code, _error_detail(response))
            self.last_headers = dict(response.headers)
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if "error" in event:
                    raise GatewayError(502, event["error"])
                for choice in event.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        yield delta

    # -- memory (pre/post hook primitives) ---------------------------------------------
    def context(
        self,
        query: str = "",
        *,
        session_id: str | None = None,
        recent_limit: int | None = None,
        related_limit: int | None = None,
        important_limit: int | None = None,
    ) -> dict[str, Any]:
        body = {
            "session_id": self._session(session_id),
            "query": query,
            "recent_limit": recent_limit,
            "related_limit": related_limit,
            "important_limit": important_limit,
        }
        return self._request("POST", "/v1/memory/context", json=body).json()

    def record_event(
        self,
        content: str,
        *,
        role: str = "note",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        link_to: str | None = None,
        relation: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "session_id": self._session(session_id),
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "link_to": link_to,
            "relation": relation,
        }
        return self._request("POST", "/v1/memory/events", json=body).json()

    def forget(
        self,
        match: str | None = None,
        *,
        ids: list[str] | None = None,
        role: str | None = None,
        session_id: str | None = None,
        with_answers: bool = True,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        body = {
            "session_id": self._session(session_id),
            "ids": ids or [],
            "match": match,
            "role": role,
            "with_answers": with_answers,
            "dry_run": dry_run,
        }
        return self._request("POST", "/v1/memory/forget", json=body).json()

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/memory/sessions", params={"limit": limit}).json()["sessions"]

    def session_graph(self, session_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        return self._request("GET", f"/v1/memory/sessions/{self._session(session_id)}", params={"limit": limit}).json()

    # -- agent-tool hooks --------------------------------------------------------------
    def hook(self, tool: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/v1/hooks/{tool}/{event}", json=payload).json()


def _gateway_headers(session: str, memory: bool, job: str | None, budget_usd: float | None) -> dict[str, str]:
    headers = {"X-Session-Id": session, "X-Memory": "on" if memory else "off"}
    if job:
        headers["X-Job"] = job
    if budget_usd is not None:
        if budget_usd < 0:
            raise ValueError("budget_usd must be zero or positive")
        # Gateway applies this only as a cap: it can lower the key's daily budget, never raise it.
        headers["X-Budget-USD"] = f"{budget_usd:.4f}"
    return headers


def _error_detail(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    return body.get("detail") or body.get("error") or body
