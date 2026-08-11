#!/usr/bin/env bash
# Initial setup: virtualenv, dependencies, data dirs, .env scaffold.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Instagram Analyzer setup"

# 1. Python check
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.9+ first." >&2
  exit 1
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "ERROR: Python 3.9+ required (zoneinfo). Found: $(python3 --version)" >&2
  exit 1
}
echo "    Python: $(python3 --version)"

# 2. Virtualenv
if [ ! -d venv ]; then
  echo "==> Creating virtualenv (venv/)"
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

# 3. Dependencies
echo "==> Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 4. Data directories
echo "==> Creating data directories"
mkdir -p data/logs data/analyses data/raw

# 5. .env scaffold
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from template — edit it with your tokens:"
  echo "        nano .env"
else
  echo "    .env already exists — leaving it untouched."
fi

echo ""
echo "Setup complete. Next:"
echo "  1. Edit .env with your IG_TOKEN, IG_USER_ID and ANTHROPIC_API_KEY"
echo "  2. Check collection without spending Claude credits:"
echo "         source venv/bin/activate && python main.py --dry-run"
echo "  3. Full run: python main.py"
echo "  4. Schedule: ./cron-setup.sh"
