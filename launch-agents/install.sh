#!/bin/bash
# Install (or refresh) the two LaunchAgents that automate the
# Next Gen reviewer reflection pipeline:
#
#   com.dwell.whisper-server   — keeps the local Whisper server running
#   com.dwell.nextgen-pickup   — runs process_recordings.py every 15 min
#
# Idempotent. Safe to re-run after editing scripts or paths — it will
# unload the existing agents (if any) and load fresh ones.
#
# Usage:
#   ./install.sh
#
# To disable later:
#   ./install.sh --uninstall

set -e

# Resolve project root from this script's location.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS"

WHISPER_PLIST="$LAUNCH_AGENTS/com.dwell.whisper-server.plist"
PICKUP_PLIST="$LAUNCH_AGENTS/com.dwell.nextgen-pickup.plist"

unload_if_loaded() {
    local label="$1"
    local plist="$2"
    if launchctl list | grep -q "$label"; then
        echo "  unloading existing $label"
        launchctl unload "$plist" 2>/dev/null || true
    fi
}

if [ "$1" = "--uninstall" ]; then
    echo "Uninstalling Dwell NextGen LaunchAgents…"
    unload_if_loaded "com.dwell.nextgen-pickup" "$PICKUP_PLIST"
    unload_if_loaded "com.dwell.whisper-server" "$WHISPER_PLIST"
    rm -f "$WHISPER_PLIST" "$PICKUP_PLIST"
    echo "Done. (Project files in $PROJECT_DIR are untouched.)"
    exit 0
fi

# ---------------------------------------------------------------------
# Pre-flight — make sure the binaries we depend on actually exist.
# ---------------------------------------------------------------------

WHISPER_BIN="$(command -v whisper-server || true)"
if [ -z "$WHISPER_BIN" ]; then
    echo "error: whisper-server not found in PATH."
    echo "  Install with:  brew install whisper-cpp"
    exit 1
fi
echo "✓ whisper-server: $WHISPER_BIN"

WHISPER_MODEL="$HOME/.cache/whisper/ggml-medium.en.bin"
if [ ! -f "$WHISPER_MODEL" ]; then
    echo "error: Whisper model not found at $WHISPER_MODEL"
    echo "  Download with:  whisper-cpp-download-ggml-model medium.en"
    echo "  Then move:      mkdir -p ~/.cache/whisper && mv ~/Downloads/ggml-medium.en.bin ~/.cache/whisper/"
    exit 1
fi
echo "✓ whisper model: $WHISPER_MODEL"

# ---------------------------------------------------------------------
# Python venv — create if missing, install/upgrade deps.
# ---------------------------------------------------------------------

VENV_DIR="$PROJECT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating Python venv at $VENV_DIR…"
    python3 -m venv "$VENV_DIR"
fi

echo "Installing/updating Python deps…"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet \
    requests \
    google-auth \
    google-auth-oauthlib \
    google-api-python-client
echo "✓ Python deps installed in venv"

# ---------------------------------------------------------------------
# Render plist templates with absolute paths.
# ---------------------------------------------------------------------

render_plist() {
    local template="$1"
    local target="$2"
    sed \
        -e "s|{{WHISPER_BIN}}|$WHISPER_BIN|g" \
        -e "s|{{HOME}}|$HOME|g" \
        -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
        "$template" > "$target"
    echo "✓ wrote $target"
}

# Unload anything that's already running so we can replace it.
unload_if_loaded "com.dwell.nextgen-pickup" "$PICKUP_PLIST"
unload_if_loaded "com.dwell.whisper-server" "$WHISPER_PLIST"

render_plist "$SCRIPT_DIR/com.dwell.whisper-server.plist.template" "$WHISPER_PLIST"
render_plist "$SCRIPT_DIR/com.dwell.nextgen-pickup.plist.template" "$PICKUP_PLIST"

# ---------------------------------------------------------------------
# Load 'em.
# ---------------------------------------------------------------------

echo "Loading LaunchAgents…"
launchctl load "$WHISPER_PLIST"
launchctl load "$PICKUP_PLIST"

# Quick health check — give whisper-server a few seconds to bind, then
# probe the port.
sleep 3
if curl -s -m 2 -o /dev/null -w "%{http_code}" http://127.0.0.1:12017/v1/models | grep -q "200"; then
    echo "✓ whisper-server responding on 127.0.0.1:12017"
else
    echo "⚠ whisper-server not yet responding — check /tmp/dwell-whisper-server.err"
    echo "  (launch can take a few seconds; it may come up shortly.)"
fi

cat <<EOF

Done.

  pickup runs every 15 min while your Mac is awake.
  whisper-server stays running in the background.

  Logs:
    tail -f /tmp/dwell-nextgen-pickup.log
    tail -f /tmp/dwell-whisper-server.log

  Run pickup once manually right now (skip waiting):
    launchctl start com.dwell.nextgen-pickup

  Run pickup directly with full output (good for debugging):
    "$VENV_DIR/bin/python3" "$PROJECT_DIR/jenny-skill/scripts/process_recordings.py"

  Disable both later:
    "$SCRIPT_DIR/install.sh" --uninstall
EOF
