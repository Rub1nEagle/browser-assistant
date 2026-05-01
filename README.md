# browser-assistant

Autonomous AI agent that drives a real Chromium browser to solve open-ended natural-language tasks. The agent observes pages via Playwright's accessibility snapshot, reasons with an LLM (Claude or GPT), and acts through a small set of tools — no per-site selectors or scripted action sequences.

## Status

**Phases 1–5 implemented.** Walking skeleton, full tool surface, context compression, scratchpad, post-action reflection, repeated-action detector, Docker + noVNC + persistent profile + login wizard, Web UI with embedded noVNC. Phase 6 (replay tests + provider polish) is queued. Plan: `/Users/arcadnick/.claude/plans/browser-ai-agent-logical-turing.md`.

## Two ways to run

**1. Docker (recommended).** Bundles Chromium + Xvfb + x11vnc + noVNC, profile lives on a named volume so logins survive restarts, browser is visible in your tab via noVNC. Cross-platform (Linux/macOS/Windows).

**2. Local venv.** Native Chromium window on your desktop. Simpler for one-off testing, no profile persistence story beyond `./.browser-profile/`.

---

## Docker quick-start

```bash
cp .env.example .env
# Edit .env: pick LLM_PROVIDER and fill the matching API key.

docker compose up -d --build
# First build takes ~3-5 minutes (Playwright base image is large).

# Log in to the sites you'll automate (run once per site):
docker compose exec agent python -m agent login https://mail.yandex.ru
# → open http://localhost:6080 in your browser, log in via noVNC,
#   then come back to terminal and press Enter.
```

You now have two ways to drive the agent:

**Web UI (recommended)** — open **http://localhost:8000** in your host browser. You'll see the task input, live tool-call timeline + cost meter + scratchpad on the left, and the embedded noVNC view of the agent's browser on the right. Submit tasks, watch the agent work, and answer `ask_user` prompts inline.

**CLI** — same agent, same volume:
```bash
docker compose exec agent python -m agent run "прочитай последние 10 писем в яндекс почте и удали спам"
```

The CLI streams tool calls to the terminal while the browser is visible at **http://localhost:6080** (raw noVNC) or inside the Web UI's iframe.

When you're done:
```bash
docker compose down            # stop, but profile volume survives
docker compose down -v         # nuke profile too — you'll need to log in again
```

### Apple Silicon note

The arm64 Playwright image occasionally has Chromium sandbox flakes. If the browser crashes on first launch, uncomment `platform: linux/amd64` in `docker-compose.yml` — slower under Rosetta but stable.

---

## Local venv quick-start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
# (Use requirements-dev.txt instead if you want pytest/ruff.)
.venv/bin/playwright install chromium

cp .env.example .env
# Edit .env

.venv/bin/python -m agent run "open https://example.com, click the link, tell me the destination URL"
```

`requirements.txt` pins exact direct-dep versions for reproducibility. `pyproject.toml` keeps the loose library ranges; the lock-file is what builds and CI should follow.

`BROWSER_PROFILE_DIR` defaults to `./.browser-profile` — Chromium will keep cookies/storage there between runs.

---

## LLM provider

Two providers ship in v1; the OpenAI adapter doubles as a generic OpenAI-compatible client.

- **`LLM_PROVIDER=anthropic`** — uses Claude. Required: `ANTHROPIC_API_KEY`. Default model `claude-sonnet-4-6`. Set `ANTHROPIC_BASE_URL` to route through a proxy/reseller.
- **`LLM_PROVIDER=openai`** — uses GPT-class models *or* any OpenAI-compatible endpoint. Required: `OPENAI_API_KEY`. Default model `gpt-4o-mini`. Set `OPENAI_BASE_URL` for alternative endpoints:
  - OpenRouter: `https://openrouter.ai/api/v1`
  - DeepSeek: `https://api.deepseek.com`
  - Local Ollama: `http://localhost:11434/v1`

For canonical scenarios (Yandex Mail spam triage, hh.ru applications), `gpt-4o`-class or `claude-sonnet-4-6` is recommended — `gpt-4o-mini` plans poorly on >5-step flows. See `.env.example` for full config.

Other defaults:
- `MAX_STEPS=60`
- `MAX_COST_USD=2.0` (graceful abort when exceeded; set 0 to disable)
- `BROWSER_PROFILE_DIR=/data/profile` in Docker, `./.browser-profile` locally

---

## How it works

1. **CLI** wires a `BrowserController` (Playwright persistent context), an LLM client, a `ContextManager` (history + scratchpad + observe compression), a `ToolRegistry`, and an event bus into an `Agent`.
2. The agent runs a **ReAct loop**: send the user task plus tool definitions to the LLM, dispatch the returned `tool_use` blocks to handlers, append results, repeat until `done()` or step cap.
3. **`observe`** returns Playwright's `aria_snapshot(mode="ai")` — a YAML accessibility tree with `[ref=eN]` markers. The agent passes those refs to `click`/`type`, which resolve via Playwright's `aria-ref=` selector engine. Refs invalidate on the next observe.
4. **Older `observe()` snapshots in history get auto-compressed** to a one-liner — the agent always sees the latest page in full but doesn't pay the token cost for stale snapshots. Scratchpad survives compression and is appended to the system prompt every step.
5. **`extract`** is a sub-agent: a separate LLM call with its own system prompt, fed the full a11y tree, returning structured data. Cheaper and more reliable than asking the planner to parse huge snapshots.
6. **Reflection** runs after every mutating action (click/type/etc.). The system snapshots URL + title + body-text length before and after; if all three look unchanged, a `[reflection]` note is appended to the tool result so the agent re-considers.
7. **Repeated-action detector** appends a system note when the same `(tool, args)` pair fires three times in a row.

System prompt: `prompts/system.md`. Extractor prompt: `prompts/extractor.md`. Reflection logic: `src/agent/browser/telemetry.py` and `src/agent/core.py`.

---

## Layout

```
prompts/
  system.md              # Cached agent prompt
  extractor.md           # Sub-agent prompt for extract()
src/agent/
  __main__.py
  config.py              # .env-driven settings
  events.py              # Event bus
  core.py                # ReAct loop + telemetry hook + repeated-action detector
  llm/
    base.py              # LLMClient ABC
    types.py             # Block / Message / Tool
    anthropic_client.py  # Anthropic adapter (with prompt caching)
    openai_client.py     # OpenAI / OpenRouter / DeepSeek / Ollama adapter
  browser/
    controller.py        # Playwright lifecycle + per-tool helpers
    observe.py           # aria_snapshot wrapper
    telemetry.py         # Pre/post-action snapshots + reflection text
  tools/
    registry.py          # 15 tools: navigate, observe, click, type, …, done
    extract.py           # extract() sub-agent
  context/
    manager.py           # History + observe compression + scratchpad
  io/
    channel.py           # IOChannel ABC + StdinChannel for ask_user
src/cli/
  app.py                 # `python -m agent run "..."`
  login.py               # `python -m agent login <url>`
docker/
  Dockerfile             # (root) Playwright base + xvfb/x11vnc/novnc/supervisord
  docker-compose.yml     # (root)
  entrypoint.sh
  supervisord.conf
scripts/
  smoke_browser.py       # No-LLM smoke test for the browser path
```
