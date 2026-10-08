"""dsh-memory (灵枢) memory provider for Hermes Agent.

Wraps the dsh-memory MCP stdio server (``md_cg.mcp_server``) behind Hermes'
``MemoryProvider`` ABC so the agent gets persistent, white-box memory:

- ``prefetch()``      background recall injected into the next turn
- ``sync_turn()``     queue each completed turn for async ingest
- ``on_session_end()`` flush the session transcript into dsh-memory
- tool dispatch       ``mdcg_recall`` / ``mdcg_remember`` / ``mdcg_verify`` / ...

Install: drop this directory into ``$HERMES_HOME/plugins/dsh-memory`` and run
``hermes memory setup`` (select ``dsh-memory``), or set
``memory.provider: dsh-memory`` manually.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, spawn_context_thread

logger = logging.getLogger(__name__)

# Best-effort: resolve the active Hermes home for default paths. Never fatal.
def _hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser("~/.hermes")

# Poll interval (s) for the stdio MCP subprocess handshake / responses.
_POLL_S = 0.05

# Retry budget for a single MCP call before giving up.
_MAX_RETRIES = 2

# Schemas the agent sees. Keep the surface small and read/write safe.
_RECALL_SCHEMA = {
    "name": "mdcg_recall",
    "description": (
        "Recall relevant memories from dsh-memory (灵枢) for the given query. "
        "Returns ranked results with relevance scores and provenance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to recall."},
            "limit": {
                "type": "integer",
                "description": "Max results (default 5).",
            },
        },
        "required": ["query"],
    },
}

_REMEMBER_SCHEMA = {
    "name": "mdcg_remember",
    "description": (
        "Persist a fact, preference, or decision into dsh-memory (灵枢). "
        "Gated=true lets the knowledge flywheel verify before accepting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The content to remember."},
            "gated": {
                "type": "boolean",
                "description": "Run verification before accepting (default true).",
            },
            "session": {
                "type": "string",
                "description": "Session scope for this memory.",
            },
        },
        "required": ["content"],
    },
}

_VERIFY_SCHEMA = {
    "name": "mdcg_verify",
    "description": "White-box verify a candidate memory against dsh-memory.",
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id to verify."},
        },
        "required": ["node_id"],
    },
}

_SEARCH_SCHEMA = {
    "name": "mdcg_search",
    "description": "Structured search over dsh-memory nodes.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Max results (default 8)."},
        },
        "required": ["query"],
    },
}

_SCHEMAS = [_RECALL_SCHEMA, _REMEMBER_SCHEMA, _VERIFY_SCHEMA, _SEARCH_SCHEMA]

# Map tool name -> (required arg, handler(provider, args, required_value)).
_TOOLS: Dict[str, tuple] = {
    "mdcg_recall": (
        "query",
        lambda p, a, q: p._call("mdcg_recall", {"query": q, "limit": a.get("limit", 5)}),
    ),
    "mdcg_remember": (
        "content",
        lambda p, a, c: p._call(
            "mdcg_remember",
            {
                "content": c,
                "gated": a.get("gated", True),
                "session": a.get("session", p._session_id),
            },
        ),
    ),
    "mdcg_verify": (
        "node_id",
        lambda p, a, n: p._call("mdcg_verify", {"node_id": n}),
    ),
    "mdcg_search": (
        "query",
        lambda p, a, q: p._call("mdcg_search", {"query": q, "limit": a.get("limit", 8)}),
    ),
}


def _tool_error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _load_plugin_config() -> Dict[str, Any]:
    """Read provider config from config.yaml — defensive, empty on any error.

    Tries ``memory.dsh_memory`` first (matches get_config_schema keys),
    then ``plugins.dsh_memory`` (where save_config writes). Returns {}
    rather than raising so a broken/missing config never blocks discovery.
    """
    for getter in (_cfg_getter_a, _cfg_getter_b):
        try:
            cfg = getter()
            if isinstance(cfg, dict) and cfg:
                return cfg
        except Exception:
            continue
    return {}


def _cfg_getter_a() -> Optional[Dict[str, Any]]:
    from hermes_cli.config import load_config_readonly

    mem = load_config_readonly().get("memory", {})
    if isinstance(mem, dict):
        block = mem.get("dsh_memory")
        return block if isinstance(block, dict) else None
    return None


def _cfg_getter_b() -> Optional[Dict[str, Any]]:
    from hermes_cli.config import load_config_readonly

    plugins = load_config_readonly().get("plugins", {})
    if isinstance(plugins, dict):
        block = plugins.get("dsh_memory")
        return block if isinstance(block, dict) else None
    return None


class _McpStdioClient:
    """Minimal stdio MCP client: spawn, initialize, tools/call, read lines."""

    # Class-level counter (self._next_id is also set per instance for
    # readability, but _id() bumps the class attribute).
    _next_id = 0

    def __init__(self, command: List[str], env: Dict[str, str], cwd: Optional[str] = None):
        self._command = command
        self._env = {**os.environ, **env}
        self._cwd = cwd
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._inbox: "queue.Queue[Optional[str]]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._reader_proc: Optional[subprocess.Popen] = None

    def start(self) -> bool:
        with self._lock:
            if self._proc is not None:
                return True
            try:
                self._proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=self._env,
                    cwd=self._cwd,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
            except Exception as exc:
                logger.debug("dsh-memory spawn failed: %s", exc)
                self._proc = None
                return False
            if not self._handshake():
                # Release the lock first: stop() also takes self._lock, so
                # calling it while holding the lock deadlocks.
                proc = self._proc
                self._proc = None
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return False
            return True

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _handshake(self) -> bool:
        """Send ``initialize`` + ``notifications/initialized`` and wait."""
        try:
            self._send({"jsonrpc": "2.0", "id": self._id(), "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "hermes-dsh-memory", "version": "0.1.0"}}})
            resp = self._read_until(lambda r: r.get("id") is not None, timeout=15)
            if resp is None:
                return False
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return True
        except Exception as exc:
            logger.debug("dsh-memory handshake failed: %s", exc)
            return False

    def call(self, name: str, arguments: Dict[str, Any], timeout: float = 20.0) -> Optional[str]:
        """``tools/call``; returns the text content of the result or None."""
        with self._lock:
            if self._proc is None:
                return None
            req_id = self._id()
            try:
                self._send({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
            except Exception as exc:
                logger.debug("dsh-memory send failed: %s", exc)
                return None
            resp = self._read_until(lambda r: r.get("id") == req_id,
                                    timeout=timeout)
            if resp is None:
                return None
            result = resp.get("result") or {}
            if "error" in resp:
                return json.dumps({"error": resp["error"]}, ensure_ascii=False)
            content = result.get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            return "\n".join(t for t in texts if t)

    def _send(self, obj: Dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("not started")
        self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _read_until(self, predicate, timeout: float) -> Optional[Dict[str, Any]]:
        """Read stdout lines until ``predicate(line_json)`` or timeout.

        One long-lived reader thread drains stdout into ``self._inbox`` so a
        silent/hung child can never wedge the caller past ``timeout`` — and so
        consecutive calls don't fight over the pipe (a fresh reader thread per
        call would steal lines from the previous one).
        """
        if self._proc is None or self._proc.stdout is None:
            return None
        self._ensure_reader()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                return None  # child closed stdout
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if predicate(obj):
                return obj
        return None

    def _ensure_reader(self) -> None:
        """Start the single stdout-draining thread (idempotent)."""
        if self._reader is not None and self._reader.is_alive():
            if self._reader_proc is self._proc:
                return
        self._inbox = queue.Queue()
        proc = self._proc
        self._reader_proc = proc

        def _drain() -> None:
            try:
                for line in proc.stdout:
                    self._inbox.put(line)
            except Exception:
                pass
            self._inbox.put(None)

        self._reader = threading.Thread(target=_drain, daemon=True)
        self._reader.start()

    def _id(self) -> int:
        type(self)._next_id += 1
        return type(self)._next_id


class DshMemoryMemoryProvider(MemoryProvider):
    """dsh-memory (灵枢) as a Hermes memory provider over its MCP server."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or _load_plugin_config()
        self._client: Optional[_McpStdioClient] = None
        self._session_id = ""
        self._ready = False
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._writer: Optional[threading.Thread] = None
        self._stop_writer = threading.Event()
        self._prefetch_cache: Dict[str, str] = {}
        self._prefetch_result = ""
        self._last_prefetch_at = 0.0

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "dsh-memory"

    def is_available(self) -> bool:
        """Configured — cheap, no subprocess/network probe (Hermes requirement)."""
        return bool(self._server_command())

    def unavailable_reason(self) -> str:
        """User-facing hint when initialize() can't start the server."""
        if not self._server_command():
            return (
                "dsh-memory: set 'root' (dsh-memory checkout containing md_cg/) "
                "via `hermes memory setup` or memory.dsh_memory.root in config.yaml"
            )
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or ""
        cmd = self._server_command()
        if not cmd:
            logger.warning("dsh-memory: %s", self.unavailable_reason() or "no server command")
            return
        env = dict(self._config.get("env") or {})
        # Profile-scoped default so two Hermes profiles never share a store.
        hermes_home = str(kwargs.get("hermes_home") or _hermes_home())
        env.setdefault(
            "MDCG_ROOT",
            os.path.join(hermes_home, ".dsh-memory", "data", "mdcg"),
        )
        env.setdefault("MDCG_ACTOR", "hermes")
        # The plugin calls the fine-grained tool names (mdcg_recall / mdcg_remember /
        # ...). Those only exist when the server exposes the full surface; the
        # default kernel surface is just cg/stg.
        env.setdefault("MDCG_MCP_SURFACE", "full")
        # Writes need a token: without MDCG_TOKEN the store runs read-only guest
        # mode and every write returns AccessDenied. Supply it via config
        # (`token`, a secret) or inherit it from the plugin process environment.
        token = self._config.get("token") or os.environ.get("MDCG_TOKEN") or ""
        if token:
            env.setdefault("MDCG_TOKEN", str(token).strip())
        elif isinstance(self._config.get("env"), dict) and self._config["env"].get("MDCG_TOKEN"):
            pass  # already carried in via config.env
        root = str(self._config.get("root") or os.environ.get("MDCG_HOME") or "")
        if root:
            # Make `import md_cg` work inside the spawned interpreter.
            env["PYTHONPATH"] = (
                root + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else root
            )
        cwd = self._config.get("cwd") or root or None
        self._client = _McpStdioClient(cmd, env, cwd=cwd)
        self._ready = self._client.start()
        if self._ready:
            self._start_writer()
        else:
            logger.warning(
                "dsh-memory: MCP server failed to start (command=%s cwd=%s)",
                cmd, cwd,
            )

    # ------------------------------------------------------------------
    # Static prompt + prefetch
    # ------------------------------------------------------------------
    def system_prompt_block(self) -> str:
        if not self._ready:
            return ""
        return (
            "## dsh-memory (灵枢)\n"
            "Persistent white-box memory. Use mdcg_recall to recall, mdcg_remember to store, "
            "mdcg_verify / mdcg_search to audit. Prefetched context is injected per turn."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall in the background; return cached result (fast path)."""
        if not self._ready or not query:
            return ""
        now = time.monotonic()
        # Serve cache if fresh (< 30s) so repeated prefetches are cheap.
        if now - self._last_prefetch_at < 30 and self._prefetch_result:
            return self._prefetch_result
        result = self._prefetch_cache.get(query)
        if result is None:
            result = self._call("mdcg_recall", {"query": query, "limit": 5}) or ""
            self._prefetch_cache[query] = result
            # Bound the cache.
            if len(self._prefetch_cache) > 128:
                self._prefetch_cache.pop(next(iter(self._prefetch_cache)))
        self._prefetch_result = result
        self._last_prefetch_at = now
        return result

    def recall_status(self):
        """Reflect only the last prefetch (never a stale count)."""
        return None

    # ------------------------------------------------------------------
    # Per-turn sync (non-blocking queue)
    # ------------------------------------------------------------------
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue the turn for async ingest; returns immediately."""
        if not self._ready or not (user_content or assistant_content):
            return
        self._queue.put((
            session_id or self._session_id,
            user_content or "",
            assistant_content or "",
        ))

    # 单条消息的字符上限（护栏：防止巨型压缩摘要/粘贴内容撑爆节点，
    # 一条 1.5MB 的"会话要点"曾把召回预算吃掉一半）。
    _MAX_MSG_CHARS = 4000
    # 单次 session flush 的总字符上限（护栏：防止整段长对话生成超大节点）。
    _MAX_FLUSH_CHARS = 20000

    @classmethod
    def _clip(cls, text: str, limit: int) -> str:
        """Truncate ``text`` to ``limit`` chars, marking the omission."""
        text = str(text)
        if len(text) <= limit:
            return text
        return text[:limit] + f"…[已截断，原文 {len(text)} 字符]"

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush the transcript at a real session boundary."""
        if not self._ready or not messages:
            return
        parts = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue          # never persist tool/system output
            content = self._clip(m.get("content", ""), self._MAX_MSG_CHARS)
            if content.strip():
                parts.append(f"{role}: {content}")
        if not parts:
            return
        # Best-effort synchronous flush on shutdown path.
        try:
            summary = self._clip("\n".join(parts), self._MAX_FLUSH_CHARS)
            # Same as the writer path: the session triple is
            # `cg(op="session", action="note")`, not a standalone tool.
            self._call(
                "cg",
                {"op": "session", "action": "note", "summary": summary},
                timeout=30,
            )
        except Exception as exc:
            logger.debug("dsh-memory session flush failed: %s", exc)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract insights from messages about to be compressed.

        Called right before Hermes summarises the middle of the transcript.
        Two jobs, both length-bounded so a runaway transcript can never
        produce a giant memory node:

        1. Return a short insight string that Hermes folds into its own
           summary prompt (this is the documented contract).
        2. Best-effort record the *dialogue* (user/assistant only — tool
           output is dropped: it is noise for recall and bloats the store).
        """
        if not messages:
            return ""

        # --- 1) Build a bounded insight string for the summary prompt -----
        excerpt = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue                      # drop tool/system noise
            text = self._clip(m.get("content", ""), 500)
            if text.strip():
                excerpt.append(f"{role}: {text}")
        insight = self._clip("\n".join(excerpt[-20:]), 4000)

        # --- 2) Best-effort write of the pre-compress transcript ----------
        if self._ready:
            try:
                parts = []
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role", "")
                    if role not in ("user", "assistant"):
                        continue              # never persist tool output
                    content = self._clip(m.get("content", ""), self._MAX_MSG_CHARS)
                    if content.strip():
                        parts.append(f"{role}: {content}")
                summary = self._clip("\n".join(parts), self._MAX_FLUSH_CHARS)
                if summary.strip():
                    self._call(
                        "cg",
                        {"op": "session", "action": "note", "summary": summary},
                        timeout=30,
                    )
            except Exception as exc:
                logger.debug("dsh-memory pre-compress flush failed: %s", exc)

        return insight

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in MEMORY.md/USER.md writes into dsh-memory."""
        if action != "add" or not content or not self._ready:
            return
        self._queue.put((self._session_id, f"[{target}] {content}", ""))

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._ready:
            return _tool_error("dsh-memory provider not initialized")
        entry = _TOOLS.get(tool_name)
        if entry is None:
            return _tool_error(f"Unknown tool: {tool_name}")
        required, handler = entry
        value = (args or {}).get(required)
        if not value:
            return _tool_error(f"'{required}' is required")
        try:
            return handler(self, args or {}, value)
        except KeyError as exc:
            return _tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return _tool_error(str(exc))

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop_writer.set()
        if self._writer is not None:
            self._writer.join(timeout=5)
            self._writer = None
        if self._client is not None:
            self._client.stop()
            self._client = None
        self._ready = False

    # ------------------------------------------------------------------
    # Config schema / save
    # ------------------------------------------------------------------
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "python",
                "description": "Python interpreter used to run the dsh-memory MCP server",
                "default": "python",
            },
            {
                "key": "root",
                "description": "dsh-memory checkout / install path (contains md_cg/)",
                "default": "",
            },
            {
                "key": "mdcg_root",
                "description": "Memory store root (MDCG_ROOT). Empty = $HERMES_HOME/.dsh-memory/data/mdcg",
                "default": "",
            },
            {
                "key": "actor",
                "description": "Actor label written into the audit ledger (MDCG_ACTOR)",
                "default": "hermes",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to config.yaml under ``memory.dsh_memory``.

        Tries the canonical writer first; a signature mismatch falls back to a
        no-op rather than raising during ``hermes memory setup``.
        """
        clean = {k: v for k, v in (values or {}).items() if isinstance(v, str) and v}
        if not clean:
            return
        payload = {"memory": {"dsh_memory": clean}}
        try:
            from hermes_cli.config import save_config
        except Exception as exc:
            logger.debug("dsh-memory save_config import failed: %s", exc)
            return
        for kwargs in ({"merge_existing": True}, {}):
            try:
                save_config(payload, **kwargs)
                return
            except TypeError:
                continue
            except Exception as exc:
                logger.debug("dsh-memory save_config failed: %s", exc)
                return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _server_command(self) -> List[str]:
        """Build the spawn command from config, falling back to env/defaults."""
        cfg = self._config or {}
        explicit = cfg.get("command")
        if isinstance(explicit, list) and explicit:
            return [str(x) for x in explicit]
        python = str(cfg.get("python") or os.environ.get("MDCG_PYTHON") or "python")
        root = str(cfg.get("root") or os.environ.get("MDCG_HOME") or "")
        if not root:
            return []
        return [python, "-m", "md_cg.mcp_server"]

    def _call(self, name: str, arguments: Dict[str, Any], timeout: float = 20.0) -> str:
        if self._client is None:
            return ""
        for attempt in range(_MAX_RETRIES + 1):
            out = self._client.call(name, arguments, timeout=timeout)
            if out is not None:
                return out
            # Reconnect once on a dead pipe.
            if attempt < _MAX_RETRIES:
                logger.debug("dsh-memory retrying %s (attempt %d)", name, attempt + 1)
                self._client.stop()
                if not self._client.start():
                    break
        return ""

    def _start_writer(self) -> None:
        """Background daemon that drains the turn queue into dsh-memory."""
        if self._writer is not None:
            return

        def _loop() -> None:
            while not self._stop_writer.is_set():
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    session, user, assistant = item
                    payload = []
                    if user:
                        payload.append({"role": "user", "content": user[:4000]})
                    if assistant:
                        payload.append({"role": "assistant", "content": assistant[:4000]})
                    if payload:
                        summary = "\n".join(
                            f'{p["role"]}: {p["content"]}' for p in payload
                        )
                        # NB: there is no standalone `mdcg_session` tool — the
                        # session triple lives under `cg(op="session", action="note")`.
                        self._call(
                            "cg",
                            {"op": "session", "action": "note", "session": session,
                             "summary": summary},
                            timeout=30,
                        )
                except Exception as exc:
                    logger.debug("dsh-memory writer error: %s", exc)

        self._writer = spawn_context_thread(_loop, name="dsh-memory-writer")
        self._writer.daemon = True
        self._writer.start()


def register(ctx) -> None:
    """Entry point called by the memory plugin discovery system."""
    ctx.register_memory_provider(DshMemoryMemoryProvider(config=_load_plugin_config()))
