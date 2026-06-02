# recall

Find and resume lost Claude Code sessions across all projects.

`/resume` shows an abstract auto-title (locked to a session's first topic) and a
branch that deploys keep switching — neither helps you recognize a session after
a reboot loses your iTerm2 windows. `recall` instead surfaces the **verbatim
prompt trail** you typed, lets you fuzzy-search it, shows a computed
"where did I leave off" snapshot, and jumps straight back in.

## Install

```sh
make install          # copies recall.py -> ~/bin/recall
brew install fzf      # for the interactive picker (optional; falls back to --list)
```

## Usage

```sh
recall            # all projects, fuzzy picker
recall 时区       # preseed the search query
recall .          # only sessions from the current git repo / dir  (or --here)
recall --list     # plain ranked table (also the no-fzf fallback); accepts a query
recall --lang     # re-run the language chooser (中文 / English)
recall --help     # usage
```

On first use `recall` asks for a UI language (中文 / English) and remembers it in
`~/.claude/.recall-config.json`. Prompt content is never translated — only the
chrome (labels, headers, relative times).

In the picker: type to search across the whole prompt trail, the right pane
previews the trail + last assistant reply + files changed, `Enter` does
`cd <cwd> && claude -r <id>`. To quit without resuming: press `Esc` / `Ctrl-C`,
or type `exit` and pick the `✕ 退出` row.

Sessions that are **currently running** (per `~/.claude/sessions/<pid>.json`) are
marked with `●`. Picking one **jumps to its existing iTerm2 tab** (via AppleScript
+ tty match) instead of starting a duplicate `claude -r`. macOS + iTerm2 only;
other terminals / tmux fall back to a normal resume.

## How it works

Reads `~/.claude/projects/*/*.jsonl`, extracts the prompt trail, ai-title,
cwd, last branch, last assistant reply, and changed files — no model calls.
Results are cached in `~/.claude/.recall-cache.json` keyed by mtime, so repeat
runs are instant. See [docs/2026-06-02-recall-design.md](docs/2026-06-02-recall-design.md).

## Develop

```sh
make test         # python3 -m unittest
```
