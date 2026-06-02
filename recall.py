#!/usr/bin/env python3
"""recall — find and resume lost Claude Code sessions across all projects."""

import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
CACHE_PATH = os.path.expanduser("~/.claude/.recall-cache.json")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")  # live-process registry
CONFIG_PATH = os.path.expanduser("~/.claude/.recall-config.json")

# --- i18n -------------------------------------------------------------------
# All user-facing strings live here, keyed by language. Use t(key, *args).
_MSG = {
    "zh": {
        "just_now": "刚刚", "minutes": "{}分钟前", "hours": "{}小时前",
        "yesterday": "昨天", "days": "{}天前",
        "msgs": "{} 条消息", "no_prompt": "(无 prompt)",
        "live": "● 运行中 (pid {}) — Enter 跳到该窗口",
        "branch_one": "分支:", "branch_hdr": "分支 (★=对话最多):",
        "projbranch_hdr": "项目/分支 (★=对话最多):",
        "started": "开始", "last_active": "最后活动",
        "title": "标题:", "trail_hdr": "── Prompt 轨迹 (最近在最下) ──",
        "more_earlier": "  … +{} 更早",
        "whereto_hdr": "── 上次干到哪 (现算·不调模型) ──",
        "last_reply": "Claude 末回复:", "files": "改过的文件 ({}):",
        "exit_label": "退出 (exit / quit) — 不恢复任何 session",
        "exit_preview": "按 Enter 退出 recall，不恢复任何 session。\n(也可以直接按 Esc / Ctrl-C)",
        "header_hint": "Enter 恢复/跳转(●=运行中) · Esc/Ctrl-C 退出 · 输入 exit 选「退出」",
        "none_found": "没有找到任何 session",
        "cwd_gone": "原目录已不存在，无法原地恢复: {}\ntranscript: {}",
        "jumped": "已跳到正在运行的窗口 (pid {})",
        "fzf_missing": "fzf 未安装，降级为列表。安装: brew install fzf",
        "fzf_offer": "未检测到 fzf（交互选择器需要它）。现在用 brew 安装？[y/N] ",
        "fzf_installing": "正在安装 fzf…",
        "claude_missing": "找不到 claude 命令，请确认它在 PATH 上。",
    },
    "en": {
        "just_now": "just now", "minutes": "{}m ago", "hours": "{}h ago",
        "yesterday": "yesterday", "days": "{}d ago",
        "msgs": "{} msgs", "no_prompt": "(no prompt)",
        "live": "● running (pid {}) — Enter jumps to its window",
        "branch_one": "Branch:", "branch_hdr": "Branches (★=most active):",
        "projbranch_hdr": "Projects / branches (★=most active):",
        "started": "started", "last_active": "last active",
        "title": "Title:", "trail_hdr": "── Prompt trail (latest at bottom) ──",
        "more_earlier": "  … +{} earlier",
        "whereto_hdr": "── Where you left off (computed, no model) ──",
        "last_reply": "Claude's last reply:", "files": "Files changed ({}):",
        "exit_label": "exit / quit — resume nothing",
        "exit_preview": "Press Enter to quit recall without resuming.\n(or just press Esc / Ctrl-C)",
        "header_hint": "Enter resume/jump (●=running) · Esc/Ctrl-C quit · type exit to quit",
        "none_found": "No sessions found",
        "cwd_gone": "Original directory is gone; can't resume in place: {}\ntranscript: {}",
        "jumped": "Jumped to the running window (pid {})",
        "fzf_missing": "fzf not installed; falling back to list. Install: brew install fzf",
        "fzf_offer": "fzf not found (the picker needs it). Install it now with brew? [y/N] ",
        "fzf_installing": "Installing fzf…",
        "claude_missing": "Could not find the `claude` command — make sure it's on your PATH.",
    },
}
_LANG = "zh"


def set_lang(lang):
    global _LANG
    _LANG = lang if lang in _MSG else "zh"


def t(key, *args):
    msg = _MSG.get(_LANG, _MSG["zh"]).get(key) or _MSG["zh"][key]
    return msg.format(*args) if args else msg


def load_config(path=CONFIG_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(path, cfg):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except OSError:
        pass
# Bump whenever extract()'s output shape or logic changes, so stale records
# (cached under an unchanged file mtime) are invalidated and re-extracted.
CACHE_VERSION = 5

_ACK = {"ok", "y", "yes", "好", "嗯"}
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _repo_root(cwd):
    """Resolve a cwd to its project root by walking up to the first ancestor
    whose .git is a directory (the main repo). A worktree's .git is a *file*,
    so worktrees collapse to their main repo. Falls back to the nearest
    .git-file dir, else the cwd itself (dir gone / not a repo)."""
    p, fallback = cwd, None
    while True:
        g = os.path.join(p, ".git")
        if os.path.isdir(g):
            return p
        if fallback is None and os.path.exists(g):
            fallback = p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return fallback or cwd


def _project_tree(pairs, root_fn=_repo_root):
    """pairs: (cwd, branch_or_None) per user/assistant message, file order.
    Group by project (repo root), each with its branch breakdown. Projects
    sorted by message count desc, branches within each sorted by count desc."""
    projs, order, memo = {}, [], {}
    for cwd, br in pairs:
        if not cwd:
            continue
        root = memo.get(cwd)
        if root is None:
            root = memo[cwd] = root_fn(cwd)
        p = projs.get(root)
        if p is None:
            p = projs[root] = {"count": 0, "bc": {}, "border": []}
            order.append(root)
        p["count"] += 1
        if br:
            if br not in p["bc"]:
                p["bc"][br] = 0
                p["border"].append(br)
            p["bc"][br] += 1
    result = []
    for root in order:
        p = projs[root]
        branches = sorted(
            ({"name": b, "count": p["bc"][b]} for b in p["border"]),
            key=lambda x: x["count"], reverse=True)
        result.append({"name": project_short(root), "path": root,
                       "count": p["count"], "branches": branches})
    result.sort(key=lambda x: x["count"], reverse=True)
    return result


def _parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _snippet(text, limit=200):
    s = " ".join(text.split())
    return s[:limit] + ("…" if len(s) > limit else "")


def _decode_project_dir(dirpath):
    """Best-effort fallback: Claude encodes a cwd as the project dir name."""
    name = os.path.basename(dirpath)
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return dirpath


_TRAIL_CAP = 12
_HEAD_COLS = 160    # headline gets ~2 preview lines (fuller than the list column)
_TRAIL_COLS = 68    # one display line per prompt in the preview pane
_ASSIST_COLS = 140  # ~two lines for the last assistant reply
_FILES_CAP = 8


def _clean(s):
    """Collapse all whitespace (tabs, newlines) so a value is safe in one TSV cell."""
    return " ".join(str(s).split())


def _char_cols(c):
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def _truncate_cols(s, cols):
    """Collapse whitespace and cut to a display-column budget (CJK = 2 cols)."""
    s = _clean(s)
    if sum(_char_cols(c) for c in s) <= cols:
        return s
    out, w = [], 0
    for c in s:
        cw = _char_cols(c)
        if w + cw > cols - 1:  # leave one column for the ellipsis
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


def _pad(s, cols):
    """Pad (or truncate) to an exact display-column width, CJK counting as 2,
    so columns line up regardless of tab stops."""
    s = _truncate_cols(s, cols)
    return s + " " * max(0, cols - sum(_char_cols(c) for c in s))


def project_short(cwd):
    return os.path.basename(cwd.rstrip("/")) or cwd


def headline(record):
    """The session's identifier line: its first prompt (states the task — a
    better label than the last one), falling back to the ai-title."""
    if record["prompts"]:
        return record["prompts"][0]
    return record.get("ai_title") or t("no_prompt")


def abs_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


_TIME_COL, _PROJ_COL = 8, 16


def fzf_line(record, now, live_ids=()):
    """Four tab fields. fzf shows+searches 1 (an aligned time·proj·last column)
    and 2 (a search-only keyword tail) via --with-nth; 3 (id) and 4 (cwd) ride
    along for the action but aren't shown/searched. fzf can't search a field it
    doesn't show (and would reveal matches inside a hidden one anyway), so the
    keywords — branch names, ai-title, prompt trail — sit in field 2, which
    normally scrolls off-screen and only surfaces a row when it matches. A
    leading 2-col slot marks sessions that are currently running (●)."""
    reltime = relative_time(int(record["mtime"]), now)
    proj = project_short(record["cwd"])
    head = _clean(headline(record))
    marker = "● " if record["session_id"] in live_ids else "  "
    visible = f"{marker}{_pad(reltime, _TIME_COL)}  {_pad(proj, _PROJ_COL)}  {head}"
    kw = []  # project name is already searchable in `visible`, so omit it here
    for p in record.get("projects") or []:
        kw.extend(b["name"] for b in p["branches"])
    if record.get("ai_title"):
        kw.append(record["ai_title"])
    keywords = _clean(" ".join(kw) + " / " + " / ".join(record["prompts"]))
    return "\t".join([visible, keywords, record["session_id"], record["cwd"]])


def _c(s, code, on):
    """Wrap s in an ANSI SGR code when `on` (fzf's preview renders ANSI)."""
    return f"\x1b[{code}m{s}\x1b[0m" if on else s


def _branch_section(projects, color=False):
    """Render the project/branch block. One project -> just its branches;
    several -> a project→branch tree. ★ marks the most-active at each level."""
    if not projects:
        return []
    star = _c("★", "33", color)  # yellow

    def line(indent, marker_is_star, name, count):
        marker = star if marker_is_star else "·"
        return f"{indent}{marker} {name} ({count})"

    if len(projects) == 1:
        branches = projects[0]["branches"]
        if not branches:
            return []
        if len(branches) == 1:
            return [_c(t("branch_one"), "36", color) + f" {branches[0]['name']}"]
        out = [_c(t("branch_hdr"), "36", color)]
        out += [line("  ", j == 0, b["name"], b["count"])
                for j, b in enumerate(branches)]
        return out
    out = [_c(t("projbranch_hdr"), "36", color)]
    for i, p in enumerate(projects):
        marker = star if i == 0 else "·"
        name = _c(p["name"], "1", color)
        if len(p["branches"]) == 1:  # fold a lone branch onto the project line
            out.append(f"{marker} {name} ({p['count']}) · {p['branches'][0]['name']}")
        else:
            out.append(f"{marker} {name} ({p['count']})")
            out += [line("    ", j == 0, b["name"], b["count"])
                    for j, b in enumerate(p["branches"])]
    return out


def preview_text(record, now, live_pid=None, color=False):
    c = lambda s, code: _c(s, code, color)
    # lead with the first prompt as a bold, bright title — shown fuller than in
    # the (narrow) list column. Terminals can't truly enlarge the font.
    out = [c(_truncate_cols(headline(record), _HEAD_COLS), "1;97")]
    if record.get("ai_title"):  # the ai-title hugs the headline
        out.append(c(t("title"), "2") + f" {record['ai_title']}")
    out.append("")
    out.append(c(project_short(record["cwd"]), "1;36") + "  ·  "
               + c(t("msgs", record["msg_count"]), "2"))
    when = []
    if record.get("started"):
        when.append(f"{t('started')} {abs_time(record['started'])}")
    when.append(f"{t('last_active')} {relative_time(int(record['mtime']), now)}")
    out.append(c("  ·  ".join(when), "2"))
    if live_pid:
        out.append(c(t("live", live_pid), "32"))
    out.append("")  # breathing room before the project/branch section
    out += _branch_section(record.get("projects") or [], color)
    out += ["", c(t("trail_hdr"), "36")]
    prompts = record["prompts"]
    overflow = len(prompts) - _TRAIL_CAP
    if overflow > 0:
        out.append(c(t("more_earlier", overflow), "2"))
    shown = prompts[-_TRAIL_CAP:]
    for i, p in enumerate(shown):
        text = _truncate_cols(p, _TRAIL_COLS)
        if i == len(shown) - 1:
            out.append(c(f"▶ {text}", "1;32"))  # most recent: bold green
        else:
            out.append(f"· {text}")
    out += ["", c(t("whereto_hdr"), "36")]
    if record.get("last_assistant"):
        out.append(c(t("last_reply"), "2")
                   + f" {_truncate_cols(record['last_assistant'], _ASSIST_COLS)}")
    if record.get("files_changed"):
        seen, names = set(), []
        for f in record["files_changed"]:
            b = os.path.basename(f)
            if b not in seen:
                seen.add(b)
                names.append(b)
        out.append(c(t("files", len(names)), "36"))
        out += [f"  · {n}" for n in names[:_FILES_CAP]]
        if len(names) > _FILES_CAP:
            out.append(c(f"  … +{len(names) - _FILES_CAP}", "2"))
    return "\n".join(out)


def cache_load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}  # missing/legacy/older schema -> force re-extract
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def cache_save(path, cache):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "entries": cache}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def filter_here(records, here_cwd):
    base = here_cwd.rstrip("/")
    return [r for r in records
            if r["cwd"] == base or r["cwd"].startswith(base + "/")]


def searchable_text(record, now):
    """The text a query is matched against: the visible column + keyword tail
    (same fields fzf searches), so --list filtering matches the picker."""
    return " ".join(fzf_line(record, now).split("\t")[:2])


def filter_query(records, now, query):
    if not query:
        return records
    q = query.lower()
    return [r for r in records if q in searchable_text(r, now).lower()]


EXIT_ID = "__recall_exit__"


def _exit_line():
    """A sentinel picker row: choosing it quits without resuming anything."""
    visible = f"  {_pad('', _TIME_COL)}  {_pad('✕', _PROJ_COL)}  {t('exit_label')}"
    return "\t".join([visible, "exit quit 退出 q", EXIT_ID, ""])


def parse_selection(line):
    """Inverse of fzf_line: recover (session_id, cwd) from the chosen row."""
    fields = _strip_ansi(line.rstrip("\n")).split("\t")
    if len(fields) < 4:
        return (None, None)
    return (fields[2], fields[3])


def run_list(records, now, out):
    if not records:
        out.write(t("none_found") + "\n")
        return
    for r in records:
        out.write(f"cd {r['cwd']} && claude -r {r['session_id']}"
                  f"   # {relative_time(int(r['mtime']), now)} · "
                  f"{project_short(r['cwd'])} · {_clean(headline(r))}\n")


def build_index(paths, cache, extract_fn=None):
    """Return (records sorted newest-first, fresh cache).

    Reuse a cached record when the file's mtime is unchanged; re-extract on
    change; drop cache entries whose file is gone.
    """
    extract_fn = extract_fn or extract
    new_cache = {}
    items = []  # (mtime, record)
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        entry = cache.get(path)
        if entry and entry.get("mtime") == mtime:
            record = entry.get("record")
        else:
            record = extract_fn(path)
        new_cache[path] = {"mtime": mtime, "record": record}
        if record is not None:
            items.append((mtime, record))
    items.sort(key=lambda it: it[0], reverse=True)
    return [r for _, r in items], new_cache


def extract(path):
    """Parse a session JSONL file into one record dict, or None if it's empty."""
    cwd = ai_title = last_assistant = started = None
    prompts, files, seen = [], [], set()
    msg_cwb = []  # (cwd, branch) per user/assistant message, in file order
    msg_count = 0
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return None
    with f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "last-prompt":
                p = d.get("lastPrompt")
                if isinstance(p, str) and not is_junk(p):
                    p = p.strip()
                    if not prompts or prompts[-1] != p:  # collapse re-emitted dupes
                        prompts.append(p)
                continue
            if t == "ai-title":
                if d.get("aiTitle"):
                    ai_title = d["aiTitle"]
                continue
            if cwd is None and d.get("cwd"):
                cwd = d["cwd"]
            if started is None and d.get("timestamp"):
                started = _parse_ts(d["timestamp"])  # first timestamp = session start
            if t in ("user", "assistant"):
                msg_count += 1
                msg_cwb.append((d.get("cwd"), d.get("gitBranch")))
            if t == "assistant":
                for b in ((d.get("message") or {}).get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            last_assistant = _snippet(txt)
                    elif b.get("type") == "tool_use" and b.get("name") in _FILE_TOOLS:
                        fp = (b.get("input") or {}).get("file_path")
                        if fp and fp not in seen:
                            seen.add(fp)
                            files.append(fp)

    if cwd is None and not prompts and ai_title is None and \
            last_assistant is None and msg_count == 0:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if cwd is None:
        cwd = _decode_project_dir(os.path.dirname(path))
    stem = os.path.basename(path)
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    return {
        "session_id": stem,
        "path": os.path.abspath(path),
        "cwd": cwd,
        "projects": _project_tree(msg_cwb),
        "ai_title": ai_title,
        "prompts": prompts,
        "files_changed": files[:20],
        "last_assistant": last_assistant,
        "started": started,
        "mtime": mtime,
        "msg_count": msg_count,
    }


def relative_time(ts, now):
    """Human relative time in Chinese; older than a week falls back to MM-DD."""
    delta = now - ts
    if delta < 60:
        return t("just_now")
    if delta < 3600:
        return t("minutes", delta // 60)
    if delta < 86400:
        return t("hours", delta // 3600)
    if delta < 172800:
        return t("yesterday")
    if delta < 604800:
        return t("days", delta // 86400)
    return time.strftime("%m-%d", time.localtime(ts))


def is_junk(prompt):
    """True if a prompt carries no recognizable intent and should be skipped."""
    p = prompt.strip()
    if len(p) < 2:
        return True
    if p.isdigit():
        return True
    if p.lower() in _ACK:
        return True
    if p.startswith("/") and not any(c.isspace() for c in p):
        return True
    return False


def live_sessions(sessions_dir=SESSIONS_DIR):
    """sessionId -> pid for sessions whose process is still running, read from
    Claude Code's per-process registry (~/.claude/sessions/<pid>.json)."""
    out = {}
    for f in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not pid or not sid:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue          # dead
        except PermissionError:
            pass              # alive, just not ours
        except OSError:
            continue
        out[sid] = pid
    return out


def _tty_of(pid):
    try:
        r = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                           capture_output=True, text=True)
    except OSError:
        return None
    t = r.stdout.strip()
    return "/dev/" + t if t and t not in ("?", "??") else None


_ITERM_JUMP = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if tty of s is "%s" then
          select t
          select s
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
  return "notfound"
end tell
'''


def _in_iterm():
    return os.environ.get("TERM_PROGRAM") == "iTerm.app"


def jump_to_session(pid):
    """Bring the iTerm2 tab running this pid to the front. True if it jumped."""
    tty = _tty_of(pid)
    if not tty:
        return False
    try:
        r = subprocess.run(["osascript", "-e", _ITERM_JUMP % tty],
                           capture_output=True, text=True)
    except OSError:
        return False
    return r.stdout.strip() == "ok"


def session_paths(projects_root=PROJECTS_ROOT):
    return glob.glob(os.path.join(projects_root, "*", "*.jsonl"))


def index(projects_root=PROJECTS_ROOT, cache_path=CACHE_PATH):
    records, new_cache = build_index(session_paths(projects_root), cache_load(cache_path))
    cache_save(cache_path, new_cache)
    return records


def _here_root():
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2)
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getcwd()


def resume(record, live=None, jump_fn=jump_to_session):
    """If the session is already running, jump to its window instead of starting
    a duplicate; otherwise cd into its cwd and hand off to `claude -r`."""
    sid = record["session_id"]
    if live is None:
        live = live_sessions()
    if sid in live and _in_iterm() and jump_fn(live[sid]):
        sys.stderr.write(t("jumped", live[sid]) + "\n")
        return 0
    cwd = record["cwd"]
    if not os.path.isdir(cwd):
        sys.stderr.write(t("cwd_gone", cwd, record["path"]) + "\n")
        return 1
    os.chdir(cwd)
    try:
        os.execvp("claude", ["claude", "-r", sid])
    except OSError:  # claude not on PATH (e.g. an alias, or different install)
        sys.stderr.write(t("claude_missing") + "\n")
        return 127


def run_picker(records, now, query=""):
    if not records:
        sys.stderr.write(t("none_found") + "\n")
        return 0
    by_id = {r["session_id"]: r for r in records}
    live = live_sessions()
    live_ids = set(live)
    preview_dir = tempfile.mkdtemp(prefix="recall-preview-")
    try:
        for r in records:
            with open(os.path.join(preview_dir, r["session_id"]), "w", encoding="utf-8") as f:
                f.write(preview_text(r, now, live.get(r["session_id"]), color=True))
        with open(os.path.join(preview_dir, EXIT_ID), "w", encoding="utf-8") as f:
            f.write(t("exit_preview"))
        lines = "\n".join([_exit_line()] + [fzf_line(r, now, live_ids) for r in records])
        cmd = [
            "fzf", "--exact", "--delimiter=\t", "--with-nth=1,2",
            "--preview", f"cat {preview_dir}/{{3}}",
            "--preview-window=right,55%,wrap",
            "--header", t("header_hint"),
            "--prompt=recall> ", "--query", query,
        ]
        proc = subprocess.run(cmd, input=lines, text=True, stdout=subprocess.PIPE)
        sid, _ = parse_selection(proc.stdout)
    finally:
        # remove here so it's gone even when resume() replaces the process via execvp
        shutil.rmtree(preview_dir, ignore_errors=True)
    if sid and sid != EXIT_ID and sid in by_id:
        return resume(by_id[sid], live=live)
    return 0


_USAGE = """\
recall — find and resume lost Claude Code sessions across all projects.

  recall              fuzzy picker over all projects (needs fzf)
  recall <query>      open the picker with the search box pre-filled
  recall . | --here   only sessions from the current git repo / dir
  recall --list       plain ranked table (also the no-fzf fallback); accepts a query
  recall --lang       re-run the language chooser (中文 / English)
  recall --help       show this help

Language is asked once on first use and saved to ~/.claude/.recall-config.json.

In the picker: type to search (prompts, branches, project, title); Enter resumes
(cd + claude -r); Esc/Ctrl-C or the ✕ row quits.
"""


def resolve_lang(config_path=CONFIG_PATH):
    """Use the saved language; on first use ask once (TTY only) and remember it."""
    cfg = load_config(config_path)
    if cfg.get("lang") in _MSG:
        return cfg["lang"]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "zh"
    sys.stdout.write("选择语言 / Choose language:\n  1) 中文\n  2) English\n> ")
    sys.stdout.flush()
    choice = sys.stdin.readline().strip()
    lang = "en" if choice == "2" else "zh"
    cfg["lang"] = lang
    save_config(config_path, cfg)
    return lang


def _fzf_decision(has_fzf, opted_out, interactive, has_brew):
    """Pure: 'have' if fzf is present, 'ask' to offer a brew install, else
    'skip' (user opted out / non-interactive / no brew to install with)."""
    if has_fzf:
        return "have"
    if opted_out or not interactive or not has_brew:
        return "skip"
    return "ask"


def ensure_fzf(config_path=CONFIG_PATH):
    """True if fzf is available (or just got installed). On first interactive
    run without it, offer `brew install fzf`; remember a decline so we only
    nag once."""
    cfg = load_config(config_path)
    decision = _fzf_decision(
        bool(shutil.which("fzf")), cfg.get("skip_fzf_prompt"),
        sys.stdin.isatty() and sys.stdout.isatty(), bool(shutil.which("brew")))
    if decision == "have":
        return True
    if decision == "skip":
        return False
    sys.stdout.write(t("fzf_offer"))
    sys.stdout.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        cfg["skip_fzf_prompt"] = True
        save_config(config_path, cfg)
        return False
    sys.stdout.write(t("fzf_installing") + "\n")
    sys.stdout.flush()
    try:
        subprocess.run(["brew", "install", "fzf"])
    except OSError:
        pass
    return bool(shutil.which("fzf"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(_USAGE)
        return 0
    if "--lang" in argv:  # `recall --lang` re-runs the language chooser
        try:
            os.remove(CONFIG_PATH)
        except OSError:
            pass
    set_lang(resolve_lang())
    here = "--here" in argv or "." in argv
    list_mode = "--list" in argv
    query = " ".join(a for a in argv
                     if not a.startswith("-") and a != ".")

    if not list_mode and not ensure_fzf():
        sys.stderr.write(t("fzf_missing") + "\n")
        list_mode = True

    now = int(time.time())
    records = index()
    if here:
        records = filter_here(records, _here_root())

    if list_mode:
        run_list(filter_query(records, now, query), now, sys.stdout)
        return 0
    return run_picker(records, now, query)


if __name__ == "__main__":
    sys.exit(main())
