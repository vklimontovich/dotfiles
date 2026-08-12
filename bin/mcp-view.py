#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=2.0.0,<3",
# ]
# ///
"""Connect to an MCP server (handling OAuth if required) and dump everything it exposes as JSON.

EXAMPLES
    mcp-view https://use.jitsu.com/mcp                 # -t http is the default
    mcp-view https://use.jitsu.com/mcp -t sse
    mcp-view "npx -y @modelcontextprotocol/server-filesystem ~" -t stdio

    mcp-view https://use.jitsu.com/mcp | jq -r '.tools[].name'
    mcp-view https://use.jitsu.com/mcp | jq '.tools[] | select(.name=="run_sync")'

    mcp-view https://use.jitsu.com/mcp --show-auth      # cached credentials only
    mcp-view https://use.jitsu.com/mcp --reauth         # forget them and start over

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

    try:
        result = asyncio.run(inspect_server(args))
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # transports surface failures wrapped in task groups
        for e in flatten(exc):
            fail(f"{type(e).__name__}: {e}")
        return 1

    emit(result, args.compact)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # `mcp-view … | head` closes the pipe early; exit quietly the way a unix tool should.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(141) from None
