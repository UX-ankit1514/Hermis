#!/bin/zsh
set -euo pipefail
ROOT="$HOME/.keka-attendance"
printf '%s\n' 'Keka uses the persistent browser session; no Keka password is stored.'
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m keka_automation.secure_setup
