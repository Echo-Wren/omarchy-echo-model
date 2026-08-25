#!/usr/bin/env bash
# Install / re-install the hermes.openrouter bar widget on the Omarchy shell.
# Idempotent: safe to re-run after editing the plugin files.
set -euo pipefail

PLUGIN_ID="hermes.openrouter"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.config/omarchy/plugins/$PLUGIN_ID"

echo "== hermes.openrouter installer =="
echo "  source: $SRC_DIR"
echo "  dest:   $DEST"

mkdir -p "$(dirname "$DEST")"
if [[ "$SRC_DIR" != "$DEST" ]]; then
  mkdir -p "$DEST"
  cp "$SRC_DIR/manifest.json" "$SRC_DIR/Widget.qml" "$SRC_DIR/collect.py" "$DEST/" 2>/dev/null || true
fi
chmod +x "$DEST/collect.py"

# 1. Validate the manifest against the Omarchy plugin schema.
if command -v omarchy >/dev/null 2>&1; then
  echo "== validating manifest"
  omarchy plugin validate "$DEST" || { echo "manifest validation failed" >&2; exit 1; }
fi

# 2. Load the copied QML. A plugin rescan discovers new plugins but does not
# re-execute an already-loaded widget, so updates need a shell restart.
if command -v omarchy-restart-shell >/dev/null 2>&1; then
  echo "== restarting shell to load plugin code"
  omarchy-restart-shell
else
  echo "== rescanning shell plugins"
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
fi

# 3. Add the widget to the bar (right section, next to omarchy.agents).
if omarchy plugin list --json 2>/dev/null | grep -q "\"$PLUGIN_ID\""; then
  echo "== enabling bar widget"
  omarchy plugin enable "$PLUGIN_ID" >/dev/null 2>&1 || true
  omarchy bar move "$PLUGIN_ID" --after omarchy.agents >/dev/null 2>&1 || true
else
  echo "!! plugin not discovered — check for QML errors above" >&2
fi

# 4. Seed the data file so the widget renders immediately.
echo "== collecting initial data"
python3 "$DEST/collect.py"

# 5. Report.
echo
echo "done. The bar should now show a '$' icon next to omarchy.agents."
echo "Verify:"
echo "  omarchy plugin list --json | grep $PLUGIN_ID"
echo "  grep -A2 openrouter ~/.config/omarchy/shell.json"
echo
echo "If the icon is missing, reload with: omarchy-restart-shell"
echo "Remove with:                             omarchy plugin disable $PLUGIN_ID"