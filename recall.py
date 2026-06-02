#!/usr/bin/env python3
"""recall — find and resume lost Claude Code sessions across all projects."""

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
# Bump whenever extract()'s output shape or logic changes, so stale records
# (cached under an unchanged file mtime) are invalidated and re-extracted.
CACHE_VERSION = 4

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


def project_short(cwd):
    return os.path.basename(cwd.rstrip("/")) or cwd


def last_prompt_display(record):
    if record["prompts"]:
        return record["prompts"][-1]
    return record.get("ai_title") or "(无 prompt)"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


def fzf_line(record, now):
    """Six tab fields. fzf shows+searches 1..4 (time, proj, last prompt, a
    keyword column) via --with-nth; 5 (id) and 6 (cwd) are carried for the
    action but not shown/searched. fzf can't search a field it doesn't show
    (and would reveal matches inside a hidden one anyway), so the searchable
    keywords — branch/project names, ai-title, the prompt trail — live in the
    visible 4th column rather than a hidden blob."""
    reltime = relative_time(int(record["mtime"]), now)
    proj = project_short(record["cwd"])
    last = _clean(last_prompt_display(record))
    kw = []
    for p in record.get("projects") or []:
        kw.append(p["name"])
        kw.extend(b["name"] for b in p["branches"])
    if record.get("ai_title"):
        kw.append(record["ai_title"])
    keywords = _clean(" ".join(kw) + " / " + " / ".join(record["prompts"]))
    return "\t".join([reltime, proj, last, keywords,
                      record["session_id"], record["cwd"]])


def _branch_section(projects):
    """Render the project/branch block. One project -> just its branches;
    several -> a project→branch tree. ★ marks the most-active at each level."""
    if not projects:
        return []
    if len(projects) == 1:
        branches = projects[0]["branches"]
        if not branches:
            return []
        if len(branches) == 1:
            return [f"分支: {branches[0]['name']}"]
        out = ["分支 (★=对话最多):"]
        for j, b in enumerate(branches):
            out.append(f"  {'★' if j == 0 else '·'} {b['name']} ({b['count']})")
        return out
    out = ["项目/分支 (★=对话最多):"]
    for i, p in enumerate(projects):
        out.append(f"{'★' if i == 0 else '·'} {p['name']} ({p['count']})")
        for j, b in enumerate(p["branches"]):
            out.append(f"    {'★' if j == 0 else '·'} {b['name']} ({b['count']})")
    return out


def preview_text(record, now):
    out = [f"{project_short(record['cwd'])}  ·  "
           f"{relative_time(int(record['mtime']), now)}  ·  "
           f"{record['msg_count']} 条消息"]
    out += _branch_section(record.get("projects") or [])
    if record.get("ai_title"):
        out.append(f"标题: {record['ai_title']}")
    out += ["", "── Prompt 轨迹 (最近在最下) ──"]
    prompts = record["prompts"]
    overflow = len(prompts) - _TRAIL_CAP
    if overflow > 0:
        out.append(f"  … +{overflow} 更早")
    shown = prompts[-_TRAIL_CAP:]
    for i, p in enumerate(shown):
        marker = "▶" if i == len(shown) - 1 else "·"
        out.append(f"{marker} {_truncate_cols(p, _TRAIL_COLS)}")
    out += ["", "── 上次干到哪 (现算·不调模型) ──"]
    if record.get("last_assistant"):
        out.append(f"Claude 末回复: {_truncate_cols(record['last_assistant'], _ASSIST_COLS)}")
    if record.get("files_changed"):
        seen, names = set(), []
        for f in record["files_changed"]:
            b = os.path.basename(f)
            if b not in seen:
                seen.add(b)
                names.append(b)
        out.append(f"改过的文件 ({len(names)}):")
        for n in names[:_FILES_CAP]:
            out.append(f"  · {n}")
        if len(names) > _FILES_CAP:
            out.append(f"  … +{len(names) - _FILES_CAP}")
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


EXIT_ID = "__recall_exit__"


def _exit_line():
    """A sentinel picker row: choosing it quits without resuming anything."""
    return "\t".join(["", "✕", "退出 (exit / quit) — 不恢复任何 session",
                      "exit quit 退出 q", EXIT_ID, ""])


def parse_selection(line):
    """Inverse of fzf_line: strip the conceal codes, pull (session_id, cwd)."""
    fields = _strip_ansi(line.rstrip("\n")).split("\t")
    if len(fields) < 6:
        return (None, None)
    return (fields[4], fields[5])


def run_list(records, now, out):
    if not records:
        out.write("没有找到任何 session\n")
        return
    for r in records:
        out.write(f"cd {r['cwd']} && claude -r {r['session_id']}"
                  f"   # {relative_time(int(r['mtime']), now)} · "
                  f"{project_short(r['cwd'])} · {_clean(last_prompt_display(r))}\n")


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
    cwd = ai_title = last_assistant = None
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
        "mtime": mtime,
        "msg_count": msg_count,
    }


def relative_time(ts, now):
    """Human relative time in Chinese; older than a week falls back to MM-DD."""
    delta = now - ts
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    if delta < 172800:
        return "昨天"
    if delta < 604800:
        return f"{delta // 86400}天前"
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


def resume(record):
    """cd into the session's cwd and hand off to `claude -r`; warn if cwd is gone."""
    cwd, sid = record["cwd"], record["session_id"]
    if not os.path.isdir(cwd):
        sys.stderr.write(f"原目录已不存在，无法原地恢复: {cwd}\n"
                         f"transcript: {record['path']}\n")
        return 1
    os.chdir(cwd)
    os.execvp("claude", ["claude", "-r", sid])


def run_picker(records, now, query=""):
    if not records:
        sys.stderr.write("没有找到任何 session\n")
        return 0
    by_id = {r["session_id"]: r for r in records}
    preview_dir = tempfile.mkdtemp(prefix="recall-preview-")
    for r in records:
        with open(os.path.join(preview_dir, r["session_id"]), "w", encoding="utf-8") as f:
            f.write(preview_text(r, now))
    with open(os.path.join(preview_dir, EXIT_ID), "w", encoding="utf-8") as f:
        f.write("按 Enter 退出 recall，不恢复任何 session。\n(也可以直接按 Esc / Ctrl-C)")
    lines = "\n".join([_exit_line()] + [fzf_line(r, now) for r in records])
    cmd = [
        "fzf", "--exact", "--delimiter=\t", "--with-nth=1,2,3,4",
        "--preview", f"cat {preview_dir}/{{5}}",
        "--preview-window=right,55%,wrap",
        "--header", "Enter 恢复 · Esc/Ctrl-C 退出 · 输入 exit 选「退出」",
        "--prompt=recall> ", "--query", query,
    ]
    proc = subprocess.run(cmd, input=lines, text=True, stdout=subprocess.PIPE)
    sid, _ = parse_selection(proc.stdout)
    if sid and sid != EXIT_ID and sid in by_id:
        return resume(by_id[sid])
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    here = "--here" in argv or "." in argv
    list_mode = "--list" in argv
    query = " ".join(a for a in argv if not a.startswith("-") and a != ".")
    now = int(time.time())

    records = index()
    if here:
        records = filter_here(records, _here_root())

    if list_mode or not shutil.which("fzf"):
        if not list_mode:
            sys.stderr.write("fzf 未安装，降级为列表。安装: brew install fzf\n")
        run_list(records, now, sys.stdout)
        return 0
    return run_picker(records, now, query)


if __name__ == "__main__":
    sys.exit(main())
