#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.0.0,<3",
#   "textual>=1.0",
# ]
# ///
"""Connect to an MCP server (handling OAuth if required) and dump everything it exposes as JSON.

EXAMPLES
    mcp-view https://use.jitsu.com/mcp                 # -t http is the default
    mcp-view https://use.jitsu.com/mcp -t sse
    mcp-view "npx -y @modelcontextprotocol/server-filesystem ~" -t stdio

    mcp-view https://use.jitsu.com/mcp | jq -r '.tools[].name'
    mcp-view https://use.jitsu.com/mcp | jq '.tools[] | select(.name=="run_sync")'

    mcp-view https://use.jitsu.com/mcp --ui               # browse it in a terminal UI
    mcp-view https://use.jitsu.com/mcp --show-auth        # cached credentials only
    mcp-view https://use.jitsu.com/mcp --reauth           # forget them and start over

TERMINAL UI
    --ui opens a browser instead of printing: a filterable list of every tool,
    prompt, resource and resource template on the left, full detail on the right —
    description, annotation flags, every schema expanded property by property with
    types, enums, constraints and required markers, nested objects and combinators
    included, plus the entry's complete raw JSON underneath. Fields this tool has
    never heard of are shown too, so a server extension is never silently dropped.

      /  filter        r  raw JSON only     y  copy entry as JSON
      e  expand all    q  quit              esc  clear the filter

OUTPUT
    One JSON document on stdout; progress and errors on stderr, so `| jq` just works.

      { target, transport, protocolVersion, serverInfo, capabilities, instructions,
        tools[], prompts[], resources[], resourceTemplates[] }

    Keys are wire-format camelCase and every field the server sent is kept, including
    each tool's full inputSchema, annotations and outputSchema. A section the server
    does not implement is null rather than [], so "none" is distinguishable from
    "not offered". --show-auth replaces this document entirely (see below).

AUTHENTICATION
    On a 401 the client discovers the authorization server, registers itself
    dynamically, opens your browser and catches the redirect on a loopback port.
    Nothing to configure for a standards-compliant server.

    What gets cached, per server URL, in ~/.cache/mcp-view/<host>-<hash>.json (mode 0600):

      client_info   the dynamically registered OAuth client (client_id + secret)
      tokens        access_token, refresh_token, scope, expires_in
      obtained_at   when the tokens were issued, to turn expires_in into a deadline

    Later runs reuse that file and never open a browser. Once the access token
    expires the refresh token is redeemed automatically and the file is rewritten.

      --show-auth   print ONLY the cached registration and tokens, with JWTs decoded
                    and expiries resolved (PRINTS LIVE SECRETS). No connection is
                    made, no server is contacted, no tools are listed and no token is
                    refreshed — it reports what you hold right now. With nothing
                    cached it tells you to authorize first and exits 2
      --reauth      forget the cache and run the browser flow again — use after
                    revoking access, when switching accounts, or if refresh fails
      --no-cache    hold tokens in memory only; every run re-authorizes
      --no-auth     make no attempt at OAuth (pair with -H to bring your own token)

    The redirect URI is registered as http://localhost:3030/callback. --port moves it;
    if 3030 is busy a random port is used instead, which only matters on a run that
    actually needs the browser, since the registered URI must match.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import http.server
import json
import os
import re
import shlex
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

PROG = "mcp-view"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mcp-view"


# --------------------------------------------------------------------------- colour


def use_color(stream: Any) -> bool:
    """Colour a stream only when a terminal is watching, honouring NO_COLOR and FORCE_COLOR."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


BOLD, DIM, RESET = "1", "2", "\033[0m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "31", "32", "33", "34", "35", "36"

COLOR_ERR = use_color(sys.stderr)
COLOR_OUT = use_color(sys.stdout)


def tint(text: str, *codes: str, enabled: bool = True) -> str:
    if not enabled or not codes or not text:
        return text
    return f"\033[{';'.join(codes)}m{text}{RESET}"


def log(msg: str, *codes: str) -> None:
    print(tint(msg, *codes, enabled=COLOR_ERR), file=sys.stderr)


def step(msg: str) -> None:
    """Ordinary progress: a cyan arrow, plain text."""
    log(tint("→", CYAN, enabled=COLOR_ERR) + " " + msg)


def note(msg: str) -> None:
    log("  " + msg, DIM)


def warn(msg: str) -> None:
    log(tint("!", YELLOW, enabled=COLOR_ERR) + " " + msg)


def fail(msg: str) -> None:
    log(tint("error:", BOLD, RED, enabled=COLOR_ERR) + " " + msg)


# --------------------------------------------------------------------------- OAuth


class FileTokenStorage(TokenStorage):
    """Caches the registered client and its tokens under ~/.cache/mcp-view, keyed by server URL."""

    def __init__(self, server_url: str, enabled: bool = True):
        self.enabled = enabled
        key = hashlib.sha256(server_url.encode()).hexdigest()[:16]
        host = urllib.parse.urlparse(server_url).netloc.replace(":", "_") or "server"
        self.path = CACHE_DIR / f"{host}-{key}.json"
        self._mem: dict[str, Any] = {}

    def _load(self) -> dict[str, Any]:
        if not self.enabled:
            return self._mem
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        if not self.enabled:
            self._mem = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True, by_alias=True)
        # expires_in is relative, so remember the moment of issue to make it an absolute deadline.
        data["obtained_at"] = int(time.time())
        self._save(data)

    def snapshot(self) -> dict[str, Any]:
        """Raw cache contents, for --show-auth."""
        return self._load()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True, by_alias=True)
        self._save(data)

    def clear(self) -> None:
        self._mem = {}
        with contextlib.suppress(OSError):
            self.path.unlink()


CALLBACK_PAGE = b"""<!doctype html><meta charset=utf-8><title>mcp-view</title>
<style>body{font:16px/1.5 system-ui;margin:15vh auto;max-width:30rem;text-align:center}</style>
<h2>%s</h2><p>%s</p>"""


class CallbackServer:
    """Single-shot loopback listener that captures the ?code=&state= redirect."""

    def __init__(self, port: int):
        self.result: dict[str, str] = {}
        self.done = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" in params or "error" in params:
                    outer.result = {k: v[0] for k, v in params.items()}
                    ok = "code" in params
                    title = b"Authorized" if ok else b"Authorization failed"
                    detail = (
                        b"You can close this tab and return to the terminal."
                        if ok
                        else outer.result.get("error_description", outer.result.get("error", "")).encode()
                    )
                    body = CALLBACK_PAGE % (title, detail)
                    self.send_response(200 if ok else 400)
                else:
                    body = b"waiting for the authorization redirect"
                    self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if outer.result:
                    outer.done.set()

            # Browsers speculatively preconnect: a socket opens and no request ever
            # arrives. Without a timeout that connection wedges its handler forever.
            timeout = 5

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (TimeoutError, OSError):
                    self.close_connection = True

            def log_message(self, *args):  # silence stdlib request logging
                pass

        # Threaded on purpose: a single-threaded HTTPServer serves one connection at a
        # time, so a preconnect left hanging blocks serve_forever and deadlocks shutdown().
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> CallbackServer:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    async def wait(self, timeout: float) -> AuthorizationCodeResult:
        got = await asyncio.get_running_loop().run_in_executor(None, self.done.wait, timeout)
        if not got:
            raise TimeoutError(f"no OAuth redirect received within {timeout:.0f}s")
        if "error" in self.result:
            desc = self.result.get("error_description")
            raise RuntimeError(f"authorization denied: {self.result['error']}" + (f" - {desc}" if desc else ""))
        return AuthorizationCodeResult(
            code=self.result["code"],
            state=self.result.get("state"),
            iss=self.result.get("iss"),
        )


def free_port(preferred: int) -> int:
    """Prefer the requested port — some providers pin redirect URIs — but fall back to any free one."""
    for candidate in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("could not bind a loopback port for the OAuth redirect")


def build_auth(url: str, args: argparse.Namespace, server: CallbackServer) -> OAuthClientProvider:
    redirect_uri = f"http://localhost:{server.port}/callback"

    async def redirect_handler(authorization_url: str) -> None:
        step("opening browser to authorize:")
        note(tint(authorization_url, BLUE, enabled=COLOR_ERR))
        if not webbrowser.open(authorization_url):
            note("(no browser available — open the URL above manually)")

    async def callback_handler() -> AuthorizationCodeResult:
        step(f"waiting for the redirect on {redirect_uri}")
        return await server.wait(args.timeout)

    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="mcp-view",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=args.scope,
        ),
        storage=args.storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# --------------------------------------------------------------------------- auth report


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def relative(epoch: float, now: float | None = None) -> str:
    delta = epoch - (time.time() if now is None else now)
    past, delta = delta < 0, abs(delta)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= size:
            amount = f"{delta / size:.1f}".rstrip("0").rstrip(".") + unit
            break
    else:
        amount = f"{delta:.0f}s"
    return f"{amount} ago" if past else f"in {amount}"


def stamp(epoch: float | None) -> str | None:
    """An epoch rendered as both an absolute instant and a human-scale offset."""
    if epoch is None:
        return None
    return f"{iso(epoch)} ({relative(epoch)})"


def b64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_jwt(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT's header and payload. Returns None for opaque tokens.

    The signature is not verified — we are inspecting a token we already hold,
    not accepting one from a third party.
    """
    if not token or token.count(".") != 2:
        return None
    head, payload, _sig = token.split(".")
    try:
        header = json.loads(b64url(head))
        claims = json.loads(b64url(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    readable = {
        name: stamp(claims[name])
        for name in ("exp", "iat", "nbf", "auth_time", "updated_at")
        if isinstance(claims.get(name), (int, float))
    }
    return {
        "header": header,
        "claims": claims,
        "timestamps": readable or None,
        "expired": bool(isinstance(claims.get("exp"), (int, float)) and claims["exp"] < time.time()),
    }


def auth_report(storage: FileTokenStorage) -> dict[str, Any]:
    """Everything we hold for this server: registration, live tokens, decoded expiries."""
    data = storage.snapshot()
    if not data:
        return {"cacheFile": str(storage.path), "cached": False}

    report: dict[str, Any] = {
        "cacheFile": str(storage.path) if storage.enabled else None,
        "cached": bool(storage.enabled and storage.path.exists()),
        "clientInfo": None,
        "tokens": None,
        "accessToken": None,
        "refreshToken": None,
    }

    if client := data.get("client_info"):
        info = dict(client)
        info["client_id_issued_at_readable"] = stamp(client.get("client_id_issued_at"))
        # RFC 7591: zero means the secret never expires.
        expires = client.get("client_secret_expires_at")
        info["client_secret_expires_at_readable"] = "never" if expires == 0 else stamp(expires)
        report["clientInfo"] = info

    if tokens := data.get("tokens"):
        obtained = data.get("obtained_at")
        if obtained is None:  # cached by an older run — fall back to the file's mtime
            with contextlib.suppress(OSError):
                obtained = int(storage.path.stat().st_mtime)
        block = dict(tokens)
        block["obtained_at_readable"] = stamp(obtained)
        if obtained is not None and isinstance(tokens.get("expires_in"), (int, float)):
            deadline = obtained + tokens["expires_in"]
            block["expires_at_readable"] = stamp(deadline)
            block["expired"] = deadline < time.time()
        report["tokens"] = block
        report["accessToken"] = decode_jwt(tokens.get("access_token"))
        report["refreshToken"] = decode_jwt(tokens.get("refresh_token"))

    return report


# --------------------------------------------------------------------------- inspection


def dump(model: Any) -> Any:
    """Serialize with wire-format (camelCase) keys, which is what the protocol docs use."""
    return model.model_dump(mode="json", exclude_none=True, by_alias=True)


async def collect(session: ClientSession) -> dict[str, Any]:
    """Page through every listing.

    We ask for all four regardless of the advertised capabilities — plenty of servers
    under-declare — and let an unsupported-method error stand in for "not offered",
    which surfaces as null rather than an empty list.
    """

    async def paginate(label: str, list_fn, field: str) -> list[Any] | None:
        items: list[Any] = []
        cursor: str | None = None
        try:
            while True:
                params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
                page = await list_fn(params=params)
                items.extend(getattr(page, field))
                cursor = page.next_cursor
                if not cursor:
                    break
        except Exception as e:
            warn(f"{label}: {type(e).__name__}: {e}")
            return None
        return [dump(i) for i in items]

    sections = {
        "tools": await paginate("tools", session.list_tools, "tools"),
        "prompts": await paginate("prompts", session.list_prompts, "prompts"),
        "resources": await paginate("resources", session.list_resources, "resources"),
        "resourceTemplates": await paginate(
            "resourceTemplates", session.list_resource_templates, "resource_templates"
        ),
    }
    step(", ".join(f"{k} {len(v) if v is not None else '-'}" for k, v in sections.items()))
    return sections


async def describe(session: ClientSession, args: argparse.Namespace) -> dict[str, Any]:
    init = await session.initialize()
    step(f"connected to {init.server_info.name} {init.server_info.version}")
    return {
        "target": args.target,
        "transport": args.transport,
        "protocolVersion": init.protocol_version,
        "serverInfo": dump(init.server_info),
        "capabilities": dump(init.capabilities),
        "instructions": init.instructions,
        **await collect(session),
    }


async def inspect_server(args: argparse.Namespace) -> dict[str, Any]:
    headers = dict(args.header) or None

    with contextlib.ExitStack() as stack:
        auth = None
        if args.transport in ("http", "sse") and not args.no_auth:
            callbacks = stack.enter_context(CallbackServer(free_port(args.port)))
            auth = build_auth(args.target, args, callbacks)

        if args.transport == "stdio":
            cmd = shlex.split(args.target)
            params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=dict(os.environ))
            async with stdio_client(params) as streams:
                async with ClientSession(*streams[:2]) as session:
                    return await describe(session, args)

        if args.transport == "sse":
            async with sse_client(args.target, headers=headers, auth=auth) as streams:
                async with ClientSession(*streams[:2]) as session:
                    return await describe(session, args)

        # Streamable HTTP takes its auth and headers through the httpx client.
        async with create_mcp_http_client(headers=headers, auth=auth) as http_client:
            async with streamable_http_client(args.target, http_client=http_client) as streams:
                async with ClientSession(*streams[:2]) as session:
                    return await describe(session, args)


# --------------------------------------------------------------------------- tui

TUI_CSS = """
Screen { layers: base; }
#body { height: 1fr; }
#sidebar { width: 40; border-right: solid $panel-lighten-2; }
#filter { border: none; background: $boost; padding: 0 1; height: 3; }
#nav { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
#detail-scroll { padding: 0 2 1 3; }
#detail { width: 1fr; }
"""


def run_ui(doc: dict[str, Any]) -> int:
    """Browse a fetched document in a Textual app.

    Textual and rich are imported here rather than at module scope so the plain
    CLI path does not pay for them.
    """
    from rich.console import Group
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree as RichTree
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Input, Static
    from textual.widgets import Tree as NavTree

    # JSON Schema keywords rendered inline on a property's own line.
    INLINE = ("format", "pattern", "default", "const", "minimum", "maximum",
              "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
              "minItems", "maxItems", "uniqueItems", "multipleOf", "deprecated",
              "readOnly", "writeOnly", "examples", "$comment")
    # Keywords consumed by the renderer's own structure, so not "leftovers".
    HANDLED = {"type", "properties", "required", "items", "prefixItems", "description",
               "title", "enum", "$schema", "$id", "$ref", "$defs", "definitions",
               "additionalProperties", "patternProperties", "oneOf", "anyOf", "allOf",
               "not", *INLINE}

    def type_label(schema: dict[str, Any]) -> str:
        if ref := schema.get("$ref"):
            return str(ref)
        kind = schema.get("type")
        if isinstance(kind, list):
            return " | ".join(str(k) for k in kind)
        if kind == "array":
            items = schema.get("items")
            if isinstance(items, dict):
                return f"array<{type_label(items)}>"
            return "array"
        if kind:
            return str(kind)
        for combinator in ("oneOf", "anyOf", "allOf"):
            if combinator in schema:
                return combinator
        if "enum" in schema:
            return "enum"
        if "properties" in schema:
            return "object"
        return "any"

    def add_schema(parent: Any, label: str | None, schema: Any, required: bool = False) -> None:
        """Render a schema node and everything hanging off it, recursively."""
        if not isinstance(schema, dict):
            parent.add(Text(f"{label}: {json.dumps(schema)}", style="dim"))
            return

        line = Text()
        if label:
            line.append(label, style="bold cyan")
            if required:
                line.append("*", style="bold red")
            line.append("  ")
        line.append(type_label(schema), style="italic yellow")
        if enum := schema.get("enum"):
            line.append("  ∈ " + ", ".join(json.dumps(e) for e in enum), style="magenta")
        for key in INLINE:
            if key in schema:
                line.append(f"  {key}={json.dumps(schema[key])}", style="dim")
        node = parent.add(line)

        if desc := schema.get("description") or schema.get("title"):
            node.add(Text(str(desc), style="italic dim"))

        required_names = set(schema.get("required") or [])
        for name, sub in (schema.get("properties") or {}).items():
            add_schema(node, name, sub, name in required_names)
        for name, sub in (schema.get("patternProperties") or {}).items():
            add_schema(node, f"/{name}/", sub)
        if isinstance(items := schema.get("items"), dict) and (
            items.get("properties") or items.get("enum") or items.get("$ref")
        ):
            add_schema(node, "items", items)
        for index, sub in enumerate(schema.get("prefixItems") or []):
            add_schema(node, f"[{index}]", sub)
        for combinator in ("oneOf", "anyOf", "allOf", "not"):
            branch = schema.get(combinator)
            for index, sub in enumerate(branch if isinstance(branch, list) else [branch] if branch else []):
                add_schema(node, f"{combinator}[{index}]", sub)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            add_schema(node, "additionalProperties", extra)
        elif extra is False:
            node.add(Text("additionalProperties: false", style="dim"))
        for name, sub in (schema.get("$defs") or schema.get("definitions") or {}).items():
            add_schema(node, f"$defs/{name}", sub)

        # Anything this renderer does not know about is still shown, verbatim.
        for key, value in schema.items():
            if key not in HANDLED:
                node.add(Text(f"{key}: {json.dumps(value, default=str)}", style="dim"))

    def schema_block(title: str, schema: Any) -> Any:
        tree = RichTree(Text(title, style="bold"), guide_style="dim")
        add_schema(tree, None, schema)
        return tree

    def add_json(parent: Any, label: str | None, value: Any) -> None:
        """Plain JSON as a tree — for payloads that are not JSON Schema, like capabilities."""
        head = Text()
        if label is not None:
            head.append(label, style="bold cyan")
        if isinstance(value, dict):
            if label is not None:
                head.append("")
            node = parent.add(head) if label is not None else parent
            for key, sub in value.items():
                add_json(node, key, sub)
        elif isinstance(value, list):
            node = parent.add(head) if label is not None else parent
            for index, sub in enumerate(value):
                add_json(node, f"[{index}]", sub)
        else:
            if label is not None:
                head.append("  ")
            head.append(json.dumps(value, default=str),
                        style="magenta" if isinstance(value, bool) or value is None else "yellow"
                        if not isinstance(value, str) else "green")
            parent.add(head)

    def json_block(title: str, value: Any) -> Any:
        tree = RichTree(Text(title, style="bold"), guide_style="dim")
        add_json(tree, None, value)
        return tree

    def kv_table(data: dict[str, Any]) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(overflow="fold")
        for key, value in data.items():
            rendered = value if isinstance(value, str) else json.dumps(value, default=str)
            table.add_row(key, rendered)
        return table

    def badges(item: dict[str, Any]) -> Text | None:
        """Tool annotations, as flags — including any hint the spec adds later."""
        line = Text()
        known = {
            "readOnlyHint": ("read-only", "green"),
            "destructiveHint": ("destructive", "red"),
            "idempotentHint": ("idempotent", "blue"),
            "openWorldHint": ("open-world", "magenta"),
        }
        for key, value in (item.get("annotations") or {}).items():
            if key in known:
                label, style = known[key]
                line.append(f" {label if value else 'not ' + label} ", style=f"reverse {style}" if value else "dim")
            else:
                line.append(f" {key}={json.dumps(value)} ", style="reverse yellow")
            line.append(" ")
        support = (item.get("execution") or {}).get("taskSupport")
        if support:
            line.append(f" task:{support} ", style="reverse cyan")
        return line if len(line) else None

    def render_detail(kind: str, item: dict[str, Any], raw_only: bool) -> Any:
        """Everything about one entry: nothing in `item` is allowed to go unshown."""
        parts: list[Any] = []
        title = Text()
        title.append(item.get("name") or item.get("uri") or item.get("uriTemplate") or kind, style="bold")
        if (label := item.get("title")) and label != item.get("name"):
            title.append(f"  {label}", style="dim")
        parts += [title, Text()]

        if not raw_only:
            if flags := badges(item):
                parts += [flags, Text()]
            if desc := item.get("description"):
                parts += [Text(str(desc)), Text()]

            # Scalar fields, whatever they are, minus the ones rendered elsewhere.
            structural = {"name", "title", "description", "annotations", "execution",
                          "inputSchema", "outputSchema", "arguments"}
            rest = {k: v for k, v in item.items() if k not in structural}
            if rest:
                parts += [Rule("fields", style="dim"), kv_table(rest), Text()]

            for key, label in (("inputSchema", "input schema"), ("outputSchema", "output schema")):
                if schema := item.get(key):
                    parts += [Rule(label, style="dim"), schema_block(label, schema), Text()]

            if args := item.get("arguments"):
                parts.append(Rule("arguments", style="dim"))
                table = Table.grid(padding=(0, 2))
                table.add_column(style="bold cyan", no_wrap=True)
                table.add_column(style="italic yellow", no_wrap=True)
                table.add_column(overflow="fold")
                for arg in args:
                    known = {"name", "description", "required"}
                    trailing = {k: v for k, v in arg.items() if k not in known}
                    detail = str(arg.get("description", ""))
                    if trailing:
                        detail += f"  {json.dumps(trailing, default=str)}"
                    table.add_row(arg.get("name", ""), "required" if arg.get("required") else "optional", detail)
                parts += [table, Text()]

        parts += [Rule("raw json", style="dim"),
                  Syntax(json.dumps(item, indent=2, default=str), "json",
                         theme="ansi_dark", background_color="default", word_wrap=True)]
        return Group(*parts)

    def render_server(raw_only: bool) -> Any:
        overview = {k: v for k, v in doc.items()
                    if k not in ("tools", "prompts", "resources", "resourceTemplates")}
        if raw_only:
            return Syntax(json.dumps(overview, indent=2, default=str), "json",
                          theme="ansi_dark", background_color="default", word_wrap=True)
        info = doc.get("serverInfo") or {}
        parts: list[Any] = [
            Text(f"{info.get('name', '?')}  {info.get('version', '')}", style="bold"),
            Text(),
            kv_table({k: v for k, v in overview.items() if k not in ("capabilities", "instructions")}),
            Text(),
            Rule("capabilities", style="dim"),
            json_block("capabilities", doc.get("capabilities") or {}),
            Text(),
        ]
        if instructions := doc.get("instructions"):
            parts += [Rule("instructions", style="dim"), Text(str(instructions)), Text()]
        counts = {k: len(doc[k]) if doc.get(k) is not None else "not offered"
                  for k in ("tools", "prompts", "resources", "resourceTemplates")}
        parts += [Rule("inventory", style="dim"), kv_table(counts)]
        return Group(*parts)

    SECTIONS = (("tools", "Tools"), ("prompts", "Prompts"),
                ("resources", "Resources"), ("resourceTemplates", "Resource templates"))

    def entry_label(item: dict[str, Any]) -> str:
        return str(item.get("name") or item.get("uri") or item.get("uriTemplate") or "?")

    class MCPView(App):
        CSS = TUI_CSS
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("slash", "focus_filter", "Filter"),
            ("escape", "clear_filter", "Clear filter"),
            ("r", "toggle_raw", "Raw JSON"),
            ("y", "copy", "Copy JSON"),
            ("e", "expand_all", "Expand"),
        ]

        def __init__(self) -> None:
            super().__init__()
            info = doc.get("serverInfo") or {}
            self.title = f"{info.get('name', 'mcp')} {info.get('version', '')}".strip()
            self.sub_title = str(doc.get("target", ""))
            self.raw_only = False
            self.current: tuple[str, dict[str, Any]] | None = None

        def compose(self) -> ComposeResult:
            yield Header()
            with Horizontal(id="body"):
                with Vertical(id="sidebar"):
                    yield Input(placeholder="filter…", id="filter")
                    yield NavTree("mcp", id="nav")
                with VerticalScroll(id="detail-scroll"):
                    yield Static(id="detail")
            yield Footer()

        def on_mount(self) -> None:
            tree = self.query_one("#nav", NavTree)
            tree.show_root = False
            tree.guide_depth = 2
            self.build_tree("")
            tree.focus()
            self.show(render_server(self.raw_only))

        def build_tree(self, needle: str) -> None:
            tree = self.query_one("#nav", NavTree)
            tree.clear()
            tree.root.add_leaf(Text("● server", style="bold"), data=("server", None))
            needle = needle.casefold()
            for key, label in SECTIONS:
                items = doc.get(key)
                if items is None:
                    tree.root.add_leaf(Text(f"{label} — not offered", style="dim"), data=None)
                    continue
                hits = [i for i in items
                        if not needle
                        or needle in entry_label(i).casefold()
                        or needle in str(i.get("description", "")).casefold()
                        or needle in str(i.get("title", "")).casefold()]
                branch = tree.root.add(Text(f"{label} ({len(hits)})", style="bold"), expand=True)
                for item in hits:
                    branch.add_leaf(entry_label(item), data=(key.rstrip("s"), item))

        def show(self, renderable: Any) -> None:
            self.query_one("#detail", Static).update(renderable)
            self.query_one("#detail-scroll", VerticalScroll).scroll_home(animate=False)

        def refresh_detail(self) -> None:
            if self.current is None:
                self.show(render_server(self.raw_only))
            else:
                self.show(render_detail(*self.current, self.raw_only))

        def on_tree_node_highlighted(self, event: NavTree.NodeHighlighted) -> None:
            data = event.node.data
            if data is None:
                return
            kind, item = data
            self.current = None if kind == "server" else (kind, item)
            self.refresh_detail()

        def on_input_changed(self, event: Input.Changed) -> None:
            self.build_tree(event.value)

        def action_focus_filter(self) -> None:
            self.query_one("#filter", Input).focus()

        def action_clear_filter(self) -> None:
            self.query_one("#filter", Input).value = ""
            self.query_one("#nav", NavTree).focus()

        def action_toggle_raw(self) -> None:
            self.raw_only = not self.raw_only
            self.refresh_detail()
            self.notify("raw JSON only" if self.raw_only else "full detail", timeout=2)

        def action_expand_all(self) -> None:
            self.query_one("#nav", NavTree).root.expand_all()

        def action_copy(self) -> None:
            payload = self.current[1] if self.current else doc
            self.copy_to_clipboard(json.dumps(payload, indent=2, default=str))
            self.notify("copied JSON to clipboard", timeout=2)

    MCPView().run()
    return 0


# --------------------------------------------------------------------------- cli


def flatten(exc: BaseException) -> list[BaseException]:
    """Unwrap ExceptionGroups so the real cause gets printed, not 'unhandled errors in a TaskGroup'."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for e in exc.exceptions for leaf in flatten(e)]
    return [exc]


def render_json(obj: Any, indent: int | None, level: int = 0) -> str:
    """Serialize with syntax colouring, matching json.dump's layout exactly."""
    nl = "" if indent is None else "\n"
    pad = "" if indent is None else " " * (indent * (level + 1))
    tail = "" if indent is None else " " * (indent * level)
    gap = "" if indent is None else " "
    sep = tint(",", DIM) + nl

    if isinstance(obj, dict):
        if not obj:
            return tint("{}", DIM)
        body = sep.join(
            pad + tint(json.dumps(str(k)), BLUE) + tint(":", DIM) + gap + render_json(v, indent, level + 1)
            for k, v in obj.items()
        )
        return tint("{", DIM) + nl + body + nl + tail + tint("}", DIM)
    if isinstance(obj, list):
        if not obj:
            return tint("[]", DIM)
        body = sep.join(pad + render_json(v, indent, level + 1) for v in obj)
        return tint("[", DIM) + nl + body + nl + tail + tint("]", DIM)
    if isinstance(obj, str):
        return tint(json.dumps(obj), GREEN)
    if obj is None:
        return tint("null", DIM)
    if isinstance(obj, bool):
        return tint("true" if obj else "false", MAGENTA)
    return tint(json.dumps(obj), YELLOW)


def emit(obj: Any, compact: bool) -> None:
    """Write the JSON document to stdout, coloured only when a terminal is reading it."""
    indent = None if compact else 2
    if COLOR_OUT:
        sys.stdout.write(render_json(obj, indent) + "\n")
    else:
        json.dump(obj, sys.stdout, indent=indent)
        sys.stdout.write("\n")


HEADING = re.compile(r"^([A-Z][A-Z ]{2,}|[a-z][a-z ]+:)$", re.MULTILINE)
FLAG = re.compile(r"(?<![\w-])(--?[a-zA-Z][\w-]*)")
COMMENT = re.compile(r"(#[^\n]*)$", re.MULTILINE)


def paint_help(text: str) -> str:
    """Colour a finished help string.

    Post-processing rather than a custom formatter on purpose: argparse computes its
    column widths from the plain text, so injecting escapes earlier misaligns everything.
    """
    if not COLOR_OUT:
        return text
    # Comments are lifted out first: a flag coloured inside one would end the dim span early.
    comments: list[str] = []

    def park(m: re.Match[str]) -> str:
        comments.append(m.group(1))
        return f"\0{len(comments) - 1}\0"

    text = COMMENT.sub(park, text)
    text = text.replace("usage:", tint("usage:", BOLD), 1)
    text = HEADING.sub(lambda m: tint(m.group(1), BOLD), text)
    text = FLAG.sub(lambda m: tint(m.group(1), GREEN), text)
    return re.sub(r"\0(\d+)\0", lambda m: tint(comments[int(m.group(1))], DIM), text)


class ColorParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return paint_help(super().format_help())


def show_auth(args: argparse.Namespace) -> int:
    """--show-auth: report the cached credentials and nothing else.

    Purely local — the server is never contacted, so no browser opens and no
    token is refreshed. It answers "what do I currently hold for this server".
    """
    if args.transport == "stdio":
        fail("--show-auth applies to HTTP servers; a stdio server uses no OAuth")
        return 2
    if args.no_cache or args.no_auth:
        flag = "--no-cache" if args.no_cache else "--no-auth"
        fail(f"--show-auth reads the token cache, which {flag} disables")
        return 2

    report = auth_report(args.storage)
    if not report.get("tokens"):
        if report.get("clientInfo"):
            # A registration with no tokens: the browser flow was started but never finished.
            fail(f"a client is registered for {args.target}, but no tokens were ever obtained")
        else:
            fail(f"no cached credentials for {args.target}")
        note("authorize first, then re-run with --show-auth:")
        note(f"  {PROG} " + shlex.quote(args.target))
        return 2

    warn("--show-auth: the output contains live credentials, handle accordingly")
    emit(report, args.compact)
    return 0


def parse_header(value: str) -> tuple[str, str]:
    name, sep, val = value.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected 'Name: value', got {value!r}")
    return name.strip(), val.strip()


def main() -> int:
    p = ColorParser(
        prog=PROG,
        description="Dump an MCP server's tools, prompts and resources as JSON. OAuth is handled for you.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n", 2)[2],
    )
    p.add_argument("target", help="server URL, or the command line to launch when -t stdio")
    p.add_argument("-t", "--transport", choices=("http", "sse", "stdio"), default="http",
                   help="http = Streamable HTTP (default), sse = legacy HTTP+SSE, stdio = spawn TARGET")
    p.add_argument("-H", "--header", type=parse_header, action="append", default=[],
                   metavar="'Name: value'", help="extra HTTP header; repeatable")
    p.add_argument("-c", "--compact", action="store_true", help="single-line JSON")
    p.add_argument("--ui", action="store_true",
                   help="browse the result in a terminal UI instead of printing JSON: "
                        "filterable list of every tool, prompt and resource, with schemas expanded")

    auth = p.add_argument_group(
        "authentication",
        "Tokens are cached per server URL under ~/.cache/mcp-view; see the notes below.",
    )
    auth.add_argument("--show-auth", action="store_true",
                      help="print only the cached client, tokens, decoded JWTs and resolved "
                           "expiries, without contacting the server or listing tools; prints live "
                           "credentials, and exits telling you to authorize if nothing is cached")
    auth.add_argument("--reauth", action="store_true",
                      help="discard the cached client and tokens, then authorize again in the browser")
    auth.add_argument("--no-cache", action="store_true",
                      help="keep tokens in memory only, so every run re-authorizes")
    auth.add_argument("--no-auth", action="store_true",
                      help="do not attempt OAuth at all (combine with -H to supply your own token)")
    auth.add_argument("--scope", default=None, help="OAuth scopes to request")
    auth.add_argument("--port", type=int, default=3030,
                      help="loopback port for the OAuth redirect (default 3030)")
    auth.add_argument("--timeout", type=float, default=300.0,
                      help="seconds to wait for the browser authorization (default 300)")
    if len(sys.argv) == 1:  # bare invocation reads as a request for the manual
        p.print_help()
        return 0
    args = p.parse_args()

    args.storage = FileTokenStorage(args.target, enabled=not args.no_cache)
    if args.reauth:
        step("clearing cached credentials")
        args.storage.clear()

    if args.show_auth:
        return show_auth(args)

    # Checked before connecting, so a doomed --ui never makes you sit through OAuth.
    if args.ui and not sys.stdout.isatty():
        fail("--ui needs a terminal; drop it to get JSON on stdout")
        return 2

    try:
        result = asyncio.run(inspect_server(args))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # transports surface failures wrapped in task groups
        for e in flatten(exc):
            fail(f"{type(e).__name__}: {e}")
        return 1

    if args.ui:
        return run_ui(result)

    emit(result, args.compact)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # `mcp-view … | head` closes the pipe early; exit quietly the way a unix tool should.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(141) from None
