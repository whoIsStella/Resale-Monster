#!/usr/bin/env bash
set -euo pipefail

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

echo
echo "Project environment installed."
echo "Install Codex CLI on Linux with the official installer:"
echo "  curl -fsSL https://chatgpt.com/codex/install.sh | sh"
echo
echo "Then run:"
echo "  codex"
echo "  goliath doctor"
echo "  pytest"
