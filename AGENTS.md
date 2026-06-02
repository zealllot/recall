# Installing recall (for an AI coding agent)

If a user gave you this repo URL and asked you to install `recall`, do exactly
this — it is a tiny tool, do not overthink it:

```sh
git clone https://github.com/zealllot/recall && cd recall && make install
```

That is the whole install. Then tell the user it's installed and to run `recall`.

## Facts you need (don't rediscover them)

- **One self-contained file**: `recall.py`, standard library only. There is **no
  pip install, no requirements.txt, no virtualenv, no build**. Do not create one.
- `make install` copies `recall.py` → `~/bin/recall`, `chmod +x`, and prints a
  warning with the exact `export PATH=...` line **if `~/bin` is not on PATH**.
  Relay that line to the user if it appears.
- **Requirements**: macOS, `python3` (already present on dev Macs), and Claude
  Code (a `~/.claude` directory). Nothing else.
- **Do not run `recall` yourself to "verify"** — it's an interactive fzf TUI and
  will just block or do nothing useful non-interactively. To sanity-check, run
  `recall --help` or `recall --list | head` instead.
- The interactive picker needs `fzf`. Do **not** install it for the user — on the
  user's first real run, `recall` itself offers to `brew install fzf`.
- First real run also asks the user to pick a UI language (中文 / English). Leave
  that to the user; don't answer it for them.

## If `git clone` isn't appropriate (no clone, just the script)

```sh
mkdir -p ~/bin
curl -fsSL https://raw.githubusercontent.com/zealllot/recall/main/recall.py -o ~/bin/recall
chmod +x ~/bin/recall
```

Then check `~/bin` is on PATH and tell the user the `export` line if not.
