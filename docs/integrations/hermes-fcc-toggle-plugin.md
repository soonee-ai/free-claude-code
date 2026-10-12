# /fcc on|off — a per-session FCC toggle plugin for Hermes

A Hermes plugin that lets the operator flip a chat session between FCC's free
models and the day-to-day paid provider, per session, without touching the
global default. Live path: `~/.hermes/profiles/<profile>/plugins/fcc-toggle/`
(profile gateways scan ONLY their own plugins dir, not `~/.hermes/plugins`).

```python
# __init__.py
from .plugin import register  # noqa: F401

# plugin.yaml
# name: fcc-toggle
# version: 1.0.0
# description: /fcc on|off|status - switch this session to the FCC proxy and back
# kind: standalone
```

## The full handler (v2, working)

```python
"""FCC toggle — /fcc on|off|status per-session model switch (v2: in-process)."""
import json, logging
_log = logging.getLogger("fcc-toggle")

_FCC_PROVIDER = "fcc"
_ON_MODEL = "claude-sonnet-4-20250514"
_OFF_PROVIDER = "ollama-cloud"     # the user's paid default; adapt per install
_OFF_MODEL = "glm-5.3-flash"

def _session_key() -> str:
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_KEY", "")

def _gateway_runner():
    """Live gateway runner, published as the process-wide message host."""
    try:
        from hermes_cli.plugins import get_plugin_manager
        host = get_plugin_manager()._gateway_message_injector
        if host is not None:
            return host[0]
    except Exception:
        _log.debug("gateway host not reachable", exc_info=True)
    return None

def _read_override(session_key):
    """Read the session's persisted /model override from state.db (read-only)."""
    try:
        import os as _os
        from hermes_state import SessionDB
        db = SessionDB(read_only=True)
        try:
            home = _os.environ.get("HERMES_HOME") or _os.path.expanduser("~/.hermes")
            raw = db.load_gateway_routing_entries(
                scope=_os.path.join(home, "sessions")).get(session_key)
        finally:
            db.close()
        return json.loads(raw).get("model_override") if raw else None
    except Exception:
        return None

def _gateway_runner():
    """Live gateway runner object, when handled inside the gateway process.

    The runner publishes itself as the process-wide gateway message host
    (gateway/run_inbound._install_plugin_message_injector), so the plugin
    manager holds (runner, injector) while the gateway is up.
    """
    try:
        from hermes_cli.plugins import get_plugin_manager
        host = get_plugin_manager()._gateway_message_injector
        if host is not None:
            return host[0]
    except Exception:
        _log.debug("fcc-toggle: gateway host not reachable", exc_info=True)
    return None

def _build_override() -> dict:
    """Resolve the fcc provider's runtime kwargs like the native /model does."""
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    rt = _resolve_runtime_agent_kwargs_for_provider(_FCC_PROVIDER, target_model=_ON_MODEL)
    return {
        "model": _ON_MODEL, "provider": _FCC_PROVIDER,
        "base_url": rt.get("base_url"), "api_key": rt.get("api_key"),
        "api_mode": rt.get("api_mode"), "credential_pool": rt.get("credential_pool"),
        "requested_provider": rt.get("requested_provider"),
        "max_tokens": rt.get("max_tokens"),
        "request_overrides": dict(rt.get("request_overrides") or {}),
        "capabilities": dict(rt.get("capabilities") or {}),
    }

def _do_switch(runner, session_key: str, turn_on: bool) -> str:
    if turn_on:
        override = _build_override()
        detail = f"{override.get('api_mode')} -> {override.get('base_url')}"
        note = "FCC ON - next turn routes through fcc-server (free NVIDIA NIM)."
    else:
        override = None
        detail = f"{_OFF_PROVIDER}/{_OFF_MODEL}"
        note = "FCC OFF - next turn back on the default provider."
    # Durable write-through first (sanitize keeps model/provider/base_url), then
    # the in-memory dict the next turn applies, then drop the cached agent.
    runner.session_store.set_model_override(session_key, override)
    runner._session_state(session_key).conversation.model_override = override
    runner._evict_cached_agent(session_key)
    return f"{note}\nDurable override set ({detail}); clears on /new."

def _handle_fcc(raw_args: str):
    argv = raw_args.strip().lower().split()
    sub = argv[0] if argv else "status"
    if sub == "status":
        key = _session_key()
        override = _read_override(key)
        if override and override.get("provider") == _FCC_PROVIDER:
            return f"FCC ON (model {override.get('model')})."
        return "FCC OFF - session on the default provider."
    if sub in {"on", "off"}:
        key = _session_key()
        runner = _gateway_runner()
        if runner is None:
            return "The switch runs inside the gateway (messaging chat), not the CLI."
        try:
            is_running = bool(runner._is_session_running(key))
        except Exception:
            is_running = False
        if is_running:
            return "A turn is running in this session - /fcc only works when idle."
        try:
            return _do_switch(runner, key, sub == "on")
        except Exception as e:
            _log.error("fcc-toggle: switch failed: %s", e, exc_info=True)
            return f"fcc switch failed: {type(e).__name__}: {e}"
    return "Usage: /fcc on|off|status"

def register(ctx) -> None:
    ctx.register_command("fcc", handler=_handle_fcc,
                         description="Switch this session to the FCC proxy and back",
                         args_hint="on|off|status")
```

Enable it once:

```bash
hermes -p <profile> plugins enable fcc-toggle
hermes config set plugins.entries.fcc-toggle.allow_gateway_injection true # consent for gateway host access
```

## Why NOT injection (v1 design, dead)

The first version injected the literal `/model …` line into the caller's
session via `ctx.inject_message`. It can never work: gateway-injected
MessageEvents are built with `allow_gateway_control=False`
(`gateway/run_inbound.py::_dispatch_plugin_message_injection`) and
`MessageEvent.get_command()` refuses to parse slash commands when that flag is
off (`gateway/platforms/event.py`) — the injected text falls through to the
agent as plain chat. The v2 design mutates the same session state the native
`/model` command writes, in-process, with zero LLM tokens.

## Operational notes

- `/fcc status` reads the DURABLE override (state.db `gateway_routing`,
  scope = profile sessions dir); the in-memory copy is authoritative for the
  next turn but the durable one tracks it on every switch.
- A mid-turn `/fcc` is refused (`_is_session_running`) — switching mid-turn
  would race the turn's model resolution.
- `/new` clears the override (session-scoped by design); other chats are
  unaffected.
- Plugin hot-reload without gateway restart:
  `gateway.control_socket.reload_gateway_plugins(profile_home)` over the
  profile's `gateway.sock`.
