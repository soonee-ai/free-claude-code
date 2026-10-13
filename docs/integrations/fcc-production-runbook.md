# FCC production runbook (artesnuc2 Hermes box)

Field-tested operating notes for running `fcc-server` beside a live Hermes
Agent install. Everything here was learned the hard way — verify each step
against your own box.

## Install the FCC core without the bundled agents

`scripts/install.sh` in non-interactive mode wants to install ~8 coding-agent
harnesses alongside FCC. The FCC core is a single uv line; run it directly and
pin the version:

```bash
uv tool install free-claude-code==<pinned-version> --python 3.14.7
```

Executables land in `~/.local/bin/fcc-*` (uv's isolated tool env — the Hermes
Python environment is untouched). Verify: `fcc-server --version`, and that all
entry points exist in `$(uv tool dir --bin)`.

## Security settings before first start

`~/.fcc/.env` (0600) — created on first start, so write it pre-birth:

```dotenv
FCC_CONFIG_SCHEMA=2
HOST=127.0.0.1                 # DEFAULT IS 0.0.0.0 - internet-exposed on a VPS!
PROXY_AUTH_ENABLED=true
ANTHROPIC_AUTH_TOKEN=<43-char urlsafe token>   # default "freecc" - always replace
FCC_OPEN_BROWSER=false
NVIDIA_NIM_API_KEY=nvapi-...   # upstream keys
```

`PROXY_AUTH_TOKEN` is aliased from `ANTHROPIC_AUTH_TOKEN` (FCC reads the latter;
name predates the auth scheme). Verify the bind: `ss -tlnp | grep 8082` must
show 127.0.0.1 only; wrong-token POST must 401.

## Secrets via TeleIn /secret → vault → fcc (gateway machine)

When the operator delivers provider keys through TeleIn's `/secret <label>`
secure prompts, the gateway machine's maint timer (`telein_maint.sh`) routes
them: ALL-CAPS labels become `<profile>/.env` vars; other labels go to the
Hermes credential vault (`agent.vault_store.add_item`), encrypted at rest.

Two failure modes that lose the secret (learned live):

1. The vault branch needs BOTH the hermes-agent venv interpreter AND the repo
   root on `sys.path` (`sys.path.insert(0, <hermes-agent-root>)` in the heredoc
   python). Missing path → journald shows
   `telein-secret: vault save failed: No module named 'hermes_yaml'` WHILE the
   TeleIn bubble already shows "✓ delivered" — the value was fetch-acked then
   dropped, and must be re-sent (values live in server memory only).
2. NEVER probe `getSecretUpdate` "just to check if pending" — the call IS the
   ack (fetch-once semantics). A diagnostic fetch consumes the value from
   server memory and the maint timer then sees nothing. Check pending state via
   journald only.

## Signup flow gotchas (NVIDIA build.nvidia.com automation)

- `build.nvidia.com` Login → email-first form → NVGS auto-creates a NEW
  ACCOUNT when the email is unknown (no separate signup link).
- The create form embeds **hCaptcha** (`#captchaHCaptcha`) — agents must stop
  there; automation against the challenge is the ban path. A human finishes
  captcha + email verification.
- The signup page's password fields carry `autocomplete=new-password` (+ a
  "confirm password" field): password managers DO fill those, but the Hermes
  vault-fill classifier hard-excludes both patterns (sign-IN semantics). If a
  login password was set programmatically (in the vault, never in chat), the
  workaround is: re-label the field's autocomplete client-side
  (`current-password`), let vault-fill inject the first field, then copy the
  value into the confirm field inside the DOM — the secret never crosses into
  the agent's context.
- Sign-IN (`signinPassword`, autocomplete=current-password) works with vault
  fill normally.
- Login is email-first: type the address → Next → password page on
  `login.nvgs.nvidia.com`.

## Gentle browsing discipline (WAF survival)

Human-paced navigation only for nvidia.com: 3–8s randomized gaps, dwell on
pages, `polite_pace.py` gating every navigation, one tab at a time. A CAPTCHA
or challenge wall = full stop — no retries, no fingerprint rotation. Blocks
escalate (15m → 1h → 6h → 24h).

## Free-token economics

- FCC aggregates ~61 providers; free tiers are provider-controlled and change.
  NVIDIA NIM free tier is the default catalog model.
- FCC is a FREE-tier aggregator: never put the subscription key your own
  assistant runs on into FCC (self-loop, double-burn on the same flat quota).
- Wire FCC into Hermes as a fallback-chain entry or a session toggle rail
  (docs/integrations/hermes-fcc-toggle-plugin.md), not as the global primary of
  a long cached-prefix conversation.
- Paid upstreams available in the catalog (xAI = codex_responses transport with
  its own base_url) should stay opt-in per use.

## Runtime survival

`fcc-server > /tmp/fcc-server.log 2>&1` as a background process dies with the
session/reboot; for 24/7 run under systemd --user or tmux. Log tail is the
fastest health check: `POST /v1/responses 200 OK` lines mean clients are live.

## Python env shadowing trap (fastapi.sse crash)

If `fcc-server` is started from inside an agent/managed shell, a leaked
`PYTHONPATH` pointing at a host venv shadows the uv-tool environment's own
dependencies: the host fastapi (older, no `fastapi.sse` module) wins, and
`fcc-server` dies instantly with
`ModuleNotFoundError: No module named 'fastapi.sse'` no matter how many times
you reinstall the tool env. Symptom: every start attempt crashes at the same
import inside `api/code_sessions_routes.py`.

Fix: strip the inherited env before exec. A minimal launcher:

```bash
#!/bin/sh
unset PYTHONPATH
exec ~/.local/bin/fcc-server "$@"
```

Verify isolation with
`env -u PYTHONPATH ~/.local/share/uv/tools/free-claude-code/bin/python -c 'import fastapi.sse'`.

## Multi-upstream keys and the free lanes

Multiple provider keys coexist in `~/.fcc/.env` (e.g. `NVIDIA_NIM_API_KEY`,
`OPENROUTER_API_KEY`, `XAI_API_KEY`). The catalog merges across all configured
upstreams (800+ model ids); each id is prefixed with the upstream route, e.g.
`claude-3-freecc-no-thinking/open_router/<provider>/<model>`. Practical free
lanes (verified live, 2026-10):

- `open_router/openrouter/free` — OpenRouter's own `:free` model pool; works
  on both `POST /v1/responses` (streaming-only) and `POST /v1/messages`.
- `openrouter/auto`, `openrouter/auto-beta`, and all `open_router/x-ai/grok*`
  routes return HTTP 402 unless the corresponding provider key has credits —
  a key alone is not enough; the paid free-tier gate is per-credit.
