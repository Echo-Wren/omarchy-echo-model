# Echo (omarchy bar widget)

A Quickshell bar widget for Omarchy that shows **Echo's** usage, the
**DeepSeek balance**, and a **model switcher** — all fed remotely from the
Echo usage bridge on `192.168.2.41:8643`, with no local Hermes install needed.

Fork of [sradetzky/omarchy-hermes-openrouter](https://github.com/sradetzky/omarchy-hermes-openrouter)
(MIT), retargeted from a local Hermes + OpenRouter setup to the remote Echo
bridge.

- **Balance** — DeepSeek credits remaining, with a meter showing the **used**
  fraction of the topped-up balance (grows as credits are consumed) and a
  warning under 10% remaining.
- **Usage** — today / week / all-time tokens and API calls, tokens by day
  (7 days), tokens by model (30 days), all from Echo's real `state.db`.
- **Model switcher** — curated DeepSeek model list (flash, vision-exp, chat,
  reasoner); arrows move the cursor, `Enter` applies. The switch POSTs to the
  bridge, which rewrites `model.default` on the Echo VM — new Echo sessions
  use it; running sessions keep theirs.

## Requirements

- Omarchy (Quickshell) shell.
- The Echo usage bridge reachable at `http://192.168.2.41:8643` (LAN).
- The `echo-model` script installed at `~/.local/bin/echo-model` — it carries
  the switch token and performs the POST. Install it with:
  `curl -fsS http://192.168.2.41:8643/install/echo-model.sh -o ~/.local/bin/echo-model
  && chmod 700 ~/.local/bin/echo-model`

## Install

```sh
omarchy plugin add https://github.com/Echo-Wren/omarchy-echo-model.git --enable
```

or clone and run `./install.sh`. The `E` icon lands in the right bar section.

## Usage

- **Left click** — open/close the panel
- **Middle click** — refresh now
- **Right click** — open DeepSeek usage page
- In the panel: `←`/`→` move the model cursor, `Enter` applies it,
  `r` refreshes, `Esc`/`q` closes

The panel refreshes on load, on open, and every `refreshIntervalSec`
(default 300 s).

## Data and permissions

- `collect.py` is a read-only relay: it fetches `/hermes.json` + `/models`
  from the bridge and writes `~/.local/state/echo-model/stats.json` (the
  panel) and `~/.local/state/omarchy/agents/usage/hermes.json` (the built-in
  Agents tab — this makes a separate fetch timer unnecessary).
- No API keys live in this plugin. The only authenticated action is the model
  switch, done by the `echo-model` script (token is embedded in that script,
  mode 0700).
- The balance meter shows *used / topped-up* (high-water tracked by the
  bridge); the built-in Agents tab's bar is *remaining / funded* by design.

## License

[MIT](LICENSE) © 2026 Sven Radetzky (original), fork by Echo.
