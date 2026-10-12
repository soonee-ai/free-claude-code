"""FCC toggle — /fcc on|off|status per-session model switch (v2: in-process).

v2 fix for a dead design: v1 injected the literal line `/model …` into the
session via inject_message. But gateway builds injected MessageEvents with
allow_gateway_control=False (gateway/run_inbound.py) and get_command() refuses
to parse commands when that flag is off (gateway/platforms/event.py:127), so the
injected "/model" line can NEVER dispatch as a slash command on a messaging
platform — it falls through to the agent as plain chat text.

v2 performs the switch directly, through the same paths the native /model
command uses:
  - durable part  → SessionStore.set_model_override (sanitize keeps
    model/provider/base_url; session > config precedence; survives restart;
    cleared by /new)
  - live part     → runner._session_state(key).conversation.model_override
    (the dict _apply_session_model_override consumes next turn; api_key/
    api_mode/etc resolved via gateway.run._resolve_runtime_agent_kwargs_for_provider,
    exactly like the restart-rehydrate path in run_agent_cache.py)
  - then runner._evict_cached_agent(key) so the next turn builds fresh.

Requires plugins.entries.fcc-toggle.allow_gateway_injection: true only insofar
as get_plugin_manager() publishes the live gateway host; the switch itself is
plain in-process state mutation, no injection, no LLM tokens.
"""
import json
import logging
_log = logging.getLogger("fcc-toggle")

_FCC_PROVIDER = "fcc"
_ON_MODEL = "claude-sonnet-4-20250514"
_OFF_PROVIDER = "ollama-cloud"
_OFF_MODEL = "glm-5.3-flash"

_HELP = (
    "FCC toggle — per-session model switch:\n"
    "  /fcc on      — route this session through fcc-server (free NVIDIA NIM)\n"
    "  /fcc off     — back to the default ollama-cloud model\n"
    "  /fcc status  — show which rail this session is on\n"
    "\n"
    "Session-scoped via the native model_override store (same mechanism as /model):\n"
    "/new resets it, other chats are unaffected, and it survives gateway restarts."
)


def _session_key() -> str:
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_KEY", "")


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


def _read_override(session_key: str):
    """Read this session's persisted /model override from state.db gateway_routing (read-only).

    On any failure returns None — status then reports OFF, the truthful default."""
    try:
        import os as _os
        from hermes_state import SessionDB
        db = SessionDB(read_only=True)
        try:
            home = _os.environ.get("HERMES_HOME") or _os.path.expanduser("~/.hermes")
            scope = _os.path.join(home, "sessions")
            raw = db.load_gateway_routing_entries(scope=scope).get(session_key)
        finally:
            db.close()
        if not raw:
            return None
        data = json.loads(raw)
        return data.get("model_override")
    except Exception:
        return None


def _build_override() -> dict:
    """Resolve the fcc provider's runtime kwargs the way the native /model and the
    restart-rehydrate path do (gateway.run._resolve_runtime_agent_kwargs_for_provider)."""
    from gateway.run import _resolve_runtime_agent_kwargs_for_provider
    rt = _resolve_runtime_agent_kwargs_for_provider(_FCC_PROVIDER, target_model=_ON_MODEL)
    return {
        "model": _ON_MODEL,
        "provider": _FCC_PROVIDER,
        "base_url": rt.get("base_url"),
        "api_key": rt.get("api_key"),
        "api_mode": rt.get("api_mode"),
        "credential_pool": rt.get("credential_pool"),
        "requested_provider": rt.get("requested_provider"),
        "max_tokens": rt.get("max_tokens"),
        "request_overrides": dict(rt.get("request_overrides") or {}),
        "capabilities": dict(rt.get("capabilities") or {}),
    }


def _do_switch(runner, session_key: str, turn_on: bool) -> str:
    if turn_on:
        override = _build_override()
        detail = f"{override.get('api_mode')} -> {override.get('base_url')}"
        note = "🟢 FCC ON — next turn routes through fcc-server (free NVIDIA NIM)."
    else:
        override = None
        detail = f"{_OFF_PROVIDER}/{_OFF_MODEL}"
        note = "⚪ FCC OFF — next turn back on the default provider."
    # Durable write-through first (sanitize keeps model/provider/base_url), then the
    # in-memory dict the next turn applies, then drop the cached agent.
    runner.session_store.set_model_override(session_key, override)
    runner._session_state(session_key).conversation.model_override = override
    runner._evict_cached_agent(session_key)
    return f"{note}\nDurable override set ({detail}); clears on /new."


def _handle_fcc(raw_args: str):
    argv = raw_args.strip().lower().split()
    sub = argv[0] if argv else "status"

    if sub in {"help", "-h", "--help"}:
        return _HELP

    if sub == "status":
        key = _session_key()
        if not key:
            return "No session context bound — run this inside a chat session."
        override = _read_override(key)
        if override and isinstance(override, dict) and override.get("provider") == _FCC_PROVIDER:
            return ("🟢 FCC ON — this session routes through fcc-server "
                    f"(model {override.get('model')}).")
        return "⚪ FCC OFF — session on the default provider (no FCC override in this session)."

    if sub in {"on", "off"}:
        key = _session_key()
        if not key:
            return "No session context bound — run this inside a chat session."
        runner = _gateway_runner()
        if runner is None:
            return ("No live gateway host in this process — the switch can only run "
                    "inside the gateway (messaging chat), not the CLI.")
        try:
            is_running = bool(runner._is_session_running(key))
        except Exception:
            is_running = False
        if is_running:
            return ("A turn is currently running in this session — /fcc switches only "
                    "when idle. Try again after the current turn completes.")
        try:
            return _do_switch(runner, key, sub == "on")
        except Exception as e:
            _log.error("fcc-toggle: switch failed: %s", e, exc_info=True)
            return f"fcc switch failed: {type(e).__name__}: {e}"

    return f"Unknown subcommand '{sub}'.\n\n{_HELP}"


def register(ctx) -> None:
    """Register the /fcc plugin command (runs inside the gateway process when invoked from chat)."""
    ctx.register_command("fcc", handler=_handle_fcc,
                         description="Switch this session to the FCC free proxy and back",
                         args_hint="on|off|status")