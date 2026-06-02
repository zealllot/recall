#!/usr/bin/env bash
# Install recall to ~/bin/recall. Works two ways:
#   - from a clone:   ./install.sh            (copies ./recall.py)
#   - standalone:     curl -fsSL https://raw.githubusercontent.com/zealllot/recall/main/install.sh | bash
set -euo pipefail

RAW="https://raw.githubusercontent.com/zealllot/recall/main/recall.py"
DEST="$HOME/bin/recall"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

mkdir -p "$HOME/bin"
if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/recall.py" ]; then
  cp "$SRC_DIR/recall.py" "$DEST"            # running from a clone
else
  curl -fsSL "$RAW" -o "$DEST"               # standalone: pull the single script
fi
chmod +x "$DEST"
echo "installed -> $DEST"

case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) printf '\n⚠  %s is not on your PATH.\n   Add to your shell rc (e.g. ~/.zshrc) and restart the shell:\n     export PATH="$HOME/bin:$PATH"\n' "$HOME/bin" ;;
esac

command -v fzf >/dev/null 2>&1 || \
  echo "tip: the interactive picker needs fzf — recall will offer to 'brew install fzf' on first run."
