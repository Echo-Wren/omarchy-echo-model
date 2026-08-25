# Hermes OpenRouter

A Quickshell bar widget for the Omarchy shell that puts your OpenRouter
account, Hermes usage, and your model choice in one place: a `$` bar icon
with a dashboard panel.

![preview](preview.png)

- **Balance** — live remaining credits from `openrouter.ai`, with a
  spent-of-funded meter and a warning when fewer than 10% of funded credits
  are left.
- **Usage & costs** — consolidated OpenRouter tokens and spend from the
  default Hermes profile and every named profile, for today, the last 7 days,
  and all time, plus per-day and per-model breakdowns. Summary costs come
  from OpenRouter's own billing (`/auth/key`) across the distinct keys found
  in those profiles; shared keys are queried once. If any key is unavailable,
  the cards fall back to complete local estimates rather than showing a
  partial billed total. Per-day and per-model costs are always Hermes' local
  estimates and are labelled `ESTIMATED`.
- **Model switcher** — a curated OpenRouter catalogue (deepseek, claude,
  gpt, gemini, grok, qwen, …) with per-1M pricing and context length. Pick
  one and it runs `hermes -p default config set model.default <id>`; new
  default-profile sessions use it.

## Requirements

- Omarchy (Quattro shell) — the plugin is a `bar-widget` plugin
- `python3` (the data collector; standard library only — no pip packages)
- An OpenRouter API key in `OPENROUTER_API_KEY` (environment),
  `~/.hermes/.env`, or a named profile's `.env` — used only to read billing,
  the primary account balance, and the model catalogue

## Install

```sh
omarchy plugin add https://github.com/sradetzky/omarchy-hermes-openrouter.git --enable
```

The `$` icon lands in the right bar section, next to `omarchy.agents`.

## Usage

- **Left click** — open/close the panel
- **Middle click** — refresh now
- **Right click** — open the OpenRouter credits page
- In the panel: `←`/`→` move the model cursor, `Enter` applies it,
  `r` refreshes, `Esc`/`q` closes
- IPC: `omarchy-shell hermes.openrouter open|close|toggle|refresh`
  (or `omarchy-shell shell summon hermes.openrouter '{}'`)

The panel refreshes on load, on open, and every `refreshIntervalSec`
(default **300 s**).

## Data and permissions

- Reads the default Hermes session database (`~/.hermes/state.db`) and every
  named profile database (`~/.hermes/profiles/*/state.db`) read-only, then
  consolidates their OpenRouter-only usage. Profile `.env` keys are
  deduplicated in memory and are never written to plugin state or output.
- Reads the primary key's whole-account balance and each distinct key's billed
  usage from OpenRouter's public API. Account balances are not summed because
  multiple keys can belong to the same account.
- Writes state to `$XDG_STATE_HOME/hermes-openrouter/` and an
  `omarchy.agents`-compatible usage record to
  `$XDG_STATE_HOME/omarchy/agents/usage/hermes.json`.
- Changes your configuration **only** when you click a model in the panel:
  it sets `model.default` in the default Hermes profile (`hermes -p default
  config set model.default <id>`). No install-time or background
  configuration writes, and named-profile models are not changed.
- The OpenRouter model catalogue is fetched at most once every 24 h and
  cached on disk. No data leaves your machine except API calls to
  `openrouter.ai`.

## Configure

```sh
omarchy bar move hermes.openrouter --section right
omarchy bar set hermes.openrouter refreshIntervalSec 600 --json
```

### Optional: Hermes tab in the built-in Agents panel

The collector also writes an agent-usage record, so the built-in
`omarchy.agents` dashboard can show Hermes as an extra tab. Opt in by adding
`"hermes": { "enabled": true }` to that widget's `providers` setting in
`~/.config/omarchy/shell.json`.

## Remove

```sh
omarchy plugin remove hermes.openrouter
```

## License

[MIT](LICENSE) © 2026 Sven Radetzky