#!/bin/zsh
set -euo pipefail
ROOT="$HOME/.keka-attendance"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m keka_automation.secure_setup
