# recall — design

- Date: 2026-06-02
- Status: approved (pending implementation)

## Problem

I run many concurrent Claude Code sessions across iTerm2 windows. My Mac is
laggy, so I reboot periodically and sometimes lose the iTerm2 windows, which
means I lose track of which sessions I was working in. `/resume` only helps a
little because:

- The session names it shows (`ai-title`) are abstract and get **locked to the
  first topic** of a session — a long session that drifted through ten topics
  still shows its original title.
- The branch it shows is unreliable: deployment frequently switches a session's
  branch to `release-test` etc., so branch is not a stable locator.
- It is scoped to the current project, so it cannot help me find a session whose
  window I lost when I no longer remember which project it was in.

Meanwhile the data that *would* let me recognize a session — the verbatim text
of every prompt I typed — is sitting unused in the session transcript.

## Goal

A cross-project session finder, `recall`, that surfaces the prompt trail, lets
me fuzzy-search it by content, shows a computed "where did I leave off"
snapshot, and jumps straight back into the chosen session.

### Non-goals

- **No hook.** We explicitly decided against a "summarize before the
  conversation ends" hook. The clean trigger (`SessionEnd`) does not fire when
  the Mac crashes/reboots and kills the process — exactly the sessions we most
  want to recover. The robust alternative (`Stop`) would summarize after every
  turn, which is slow and costs tokens. The prompt trail plus the computed
  snapshot below cover ~90% of recognition at zero cost.
- **No model calls.** Everything in the preview is computed directly from the
  transcript JSONL.

## Form

A single `#!/usr/bin/env python3` script. Developed in this repo as `recall`,
installed to `~/bin/recall` (already on `PATH`) by copy or symlink. Self
contained, copy to another machine and it works (any dev Mac has `python3`).

On selection the script does `os.chdir(cwd)` then `os.execvp("claude", ["claude",
"-r", session_id])`. Because the `cd` happens inside the script process and
`execvp` replaces it, resume works correctly; after `/exit` the parent shell is
back at its original directory (accepted trade-off).

## Data source

`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. Each line is a compact
JSON object (no spaces around `:`). Relevant line types:

| line type     | fields used                                  |
|---------------|----------------------------------------------|
| `last-prompt` | `.lastPrompt` — verbatim text of each prompt I typed |
| `ai-title`    | `.aiTitle` — Claude's auto title (shown, de-emphasized) |
| `assistant`   | `.message.content[]` blocks, `type:"text"` → `.text`; `type:"tool_use"` → `.name`, `.input.file_path` |
| any message   | `.cwd`, `.gitBranch`, `.timestamp`           |

Verified facts (2026-06-02): file-path field is `input.file_path` for
`Edit`/`Write`/`MultiEdit`/`NotebookEdit`. Largest observed line ≈ 0.4 MB;
largest file ≈ 26 MB.

## Extraction

For each session file:

- **Prompt trail**: collect `.lastPrompt` in file order. `grep`-style
  prefilter on the substring `last-prompt` keeps this fast (0.02s on the 26 MB
  file) — never parse every line of a huge transcript.
- **ai-title**: last `ai-title` line's `.aiTitle`.
- **cwd**: first message line carrying `.cwd`. Fallback: decode the project
  directory name.
- **gitBranch (last)**: last message carrying `.gitBranch`. Shown
  de-emphasized and labelled "may have been switched by a deploy".
- **Last assistant reply**: last non-empty `type:"text"` block, truncated to
  ~2 lines.
- **Files changed**: unique `input.file_path` across `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit` tool_use blocks (modified only — not `Read`), capped (~10).
- **Last active**: file mtime (`stat`). Cheaper than scanning timestamps,
  equivalent in practice.
- **Message count**: number of `user`+`assistant` lines.

### Junk-prompt filter

A prompt is "junk" if, after trimming, it is: empty, a pure number, one of
`ok`/`y`/`yes`/`好`/`嗯`, a bare slash command, or length < 2. Junk prompts are
skipped in **both** the trail and the list line. The list line shows the most
recent *substantive* prompt.

## Cache

`~/.claude/.recall-cache.json`, keyed by `path + mtime`. On each run: reuse
cached records whose `(path, mtime)` is unchanged, re-extract changed files, and
prune entries whose file no longer exists. Makes repeat runs effectively
instant; the preview snapshot is computed at index time and stored in the cache,
so fzf scrolling never recomputes.

## UI (fzf)

Records sorted by mtime descending.

**List line** (visible columns): `relative-time · project-short · last
substantive prompt`. Relative time formatted as `2小时前 / 昨天 / 3天前`.

**Search**: fzf query matches against the *entire prompt trail* (carried as a
hidden column), so typing `时区` finds a session even when that was not its last
prompt.

**Preview pane** (read from cache, no recompute):

```
webapp  ·  2小时前  ·  64 条消息
分支(末次·可能已被部署切走): main
标题: Review feature request and plan

── Prompt 轨迹 (最近在最下) ──
· 看下这个需求 你规划一下
· 都OK 写spec
· 解决一下 PR#42 冲突
· 表单时区显示不对
▶ 帮我部署 staging PR#42

── 上次干到哪 (现算·不调模型) ──
Claude 末回复: 已触发 staging 部署，workflow 运行中，等 CI…
改过的文件: scheduler.go · scheduler_test.go · cmd/worker/main.go
```

**On Enter**: parse session id + cwd from the selected line, `chdir(cwd)`,
`execvp("claude", ["claude", "-r", id])`.

## CLI

| invocation        | behaviour                                              |
|-------------------|--------------------------------------------------------|
| `recall`          | all projects, interactive picker                       |
| `recall <query>`  | same, preseed the fzf query                            |
| `recall .` / `--here` | only sessions whose cwd is the current git repo / dir |
| `recall --list`   | non-interactive ranked table; also the no-fzf fallback |

## Resume edge cases

`claude -r <id>` resolves a session within its project directory, which is why
the script `chdir`s into the recorded cwd first.

- **cwd missing** (e.g. a deleted worktree): print a warning ("原目录已不存在，
  无法原地恢复") and the transcript file path so the session can still be read.
  The session stays visible in the list.

## Error handling

- **fzf not installed**: fall back to `--list` and print `brew install fzf`.
- **No sessions**: friendly message.
- **Corrupt / empty JSONL**: skip the file.
- `jq` is not required (extraction is pure Python).

## Module decomposition (each independently testable)

- `extract(path) -> record` — pure: a JSONL file → one record dict.
- `index(scope) -> [record]` — walk projects dir, apply cache, sort by mtime.
- `render(records) -> (fzf_input, preview_lookup)` — records → fzf TSV + preview.
- `main()` — parse args, orchestrate, launch claude.

## Testing

`extract` and the junk filter are pure and unit-tested against a fixture
directory of small synthetic JSONL files. `--list` is deterministic and
testable end to end. fzf interaction is verified manually. TDD during
implementation.

## Dependencies

`python3` (stdlib only), `fzf` (`brew install fzf`), BSD `stat`. No `jq`.

## Locked defaults (open to veto at spec review)

1. Default scope is **all projects**.
2. Last branch is shown but **de-emphasized**, never a primary locator.
3. Junk prompts (`ok`, numbers, bare slash commands, …) are filtered.
4. `recall .` / `--here` current-project mode is included.
5. Missing-cwd sessions: **warn + show transcript path**, keep in list.
