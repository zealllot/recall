import io
import json
import os
import tempfile
import unittest

import recall


SID = "5d6f2387-cc69-493a-b622-9bf218ec6a96"

FIXTURE_LINES = [
    {"type": "attachment", "cwd": "/repo/path", "gitBranch": "master"},
    {"type": "user", "cwd": "/repo/path", "gitBranch": "master",
     "message": {"role": "user", "content": "看下这个需求 你规划一下"}},
    {"type": "last-prompt", "lastPrompt": "看下这个需求 你规划一下"},
    {"type": "ai-title", "aiTitle": "Review requirement"},
    {"type": "assistant", "cwd": "/repo/path", "gitBranch": "master",
     "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "好的，我先看一下"}]}},
    {"type": "last-prompt", "lastPrompt": "ok"},
    {"type": "user", "cwd": "/repo/path", "gitBranch": "master",
     "message": {"role": "user", "content": "ok"}},
    {"type": "last-prompt", "lastPrompt": "帮我部署 prod"},
    {"type": "user", "cwd": "/repo/path", "gitBranch": "release-test",
     "message": {"role": "user", "content": "帮我部署 prod"}},
    {"type": "assistant", "cwd": "/repo/path", "gitBranch": "release-test",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "name": "Edit",
          "input": {"file_path": "/repo/path/scheduler.go",
                    "old_string": "a", "new_string": "b"}},
         {"type": "tool_use", "name": "Write",
          "input": {"file_path": "/repo/path/schedule_test.go",
                    "content": "..."}},
         {"type": "text", "text": "已触发部署，workflow 运行中，等 CI…"}]}},
]


def write_session(dirpath, session_id, lines):
    path = os.path.join(dirpath, session_id + ".jsonl")
    with open(path, "w") as f:
        for d in lines:
            f.write(json.dumps(d) + "\n")
    return path


class IsJunkTest(unittest.TestCase):
    def test_rejects_empty_and_whitespace(self):
        self.assertTrue(recall.is_junk(""))
        self.assertTrue(recall.is_junk("   "))

    def test_rejects_pure_numbers(self):
        self.assertTrue(recall.is_junk("1"))
        self.assertTrue(recall.is_junk("42"))

    def test_rejects_short_acknowledgements(self):
        for p in ["ok", "OK", "y", "yes", "好", "嗯"]:
            self.assertTrue(recall.is_junk(p), p)

    def test_rejects_bare_slash_command(self):
        self.assertTrue(recall.is_junk("/clear"))
        self.assertTrue(recall.is_junk("/resume"))

    def test_keeps_slash_command_with_args(self):
        self.assertFalse(recall.is_junk("/proctor fix the failing test"))

    def test_keeps_substantive_prompt(self):
        self.assertFalse(recall.is_junk("帮我部署 prod PR#1114"))
        self.assertFalse(recall.is_junk("filled JST but page shows UTC"))


class RepoRootTest(unittest.TestCase):
    def test_walks_up_to_dir_containing_dotgit(self):
        tmp = tempfile.mkdtemp()
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        sub = os.path.join(repo, "a", "b")
        os.makedirs(sub)
        self.assertEqual(recall._repo_root(sub), repo)
        self.assertEqual(recall._repo_root(repo), repo)

    def test_standalone_worktree_dotgit_file_returns_itself(self):
        tmp = tempfile.mkdtemp()
        wt = os.path.join(tmp, "wt")
        os.makedirs(wt)
        open(os.path.join(wt, ".git"), "w").close()  # worktree marker is a file
        self.assertEqual(recall._repo_root(wt), wt)

    def test_worktree_under_repo_collapses_to_main_repo(self):
        tmp = tempfile.mkdtemp()
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))               # main repo: .git is a dir
        wt = os.path.join(repo, ".claude", "worktrees", "x")
        os.makedirs(wt)
        open(os.path.join(wt, ".git"), "w").close()           # worktree: .git is a file
        self.assertEqual(recall._repo_root(wt), repo)

    def test_no_repo_returns_input(self):
        self.assertIsInstance(recall._repo_root(tempfile.mkdtemp()), str)


class ProjectTreeTest(unittest.TestCase):
    def root(self, cwd):  # fake resolver: repo root = first 2 path segments
        return "/".join(cwd.split("/")[:3])

    def test_groups_by_project_sorted_by_count_desc(self):
        pairs = [("/w/a/x", "m"), ("/w/a/y", "m"), ("/w/a", "f"), ("/w/b", "main")]
        out = recall._project_tree(pairs, self.root)
        self.assertEqual([p["name"] for p in out], ["a", "b"])  # a has 3, b has 1
        a = out[0]
        self.assertEqual(a["count"], 3)
        self.assertEqual([b["name"] for b in a["branches"]], ["m", "f"])  # m=2, f=1
        self.assertEqual(a["branches"][0]["count"], 2)

    def test_skips_none_cwd_and_handles_branchless(self):
        out = recall._project_tree([(None, "x"), ("/w/a/z", None)], self.root)
        self.assertEqual([p["name"] for p in out], ["a"])
        self.assertEqual(out[0]["count"], 1)
        self.assertEqual(out[0]["branches"], [])


class RelativeTimeTest(unittest.TestCase):
    NOW = 1_000_000  # fixed reference "now" in epoch seconds

    def rel(self, seconds_ago):
        return recall.relative_time(self.NOW - seconds_ago, self.NOW)

    def test_just_now(self):
        self.assertEqual(self.rel(5), "刚刚")

    def test_minutes(self):
        self.assertEqual(self.rel(5 * 60), "5分钟前")

    def test_hours(self):
        self.assertEqual(self.rel(3 * 3600), "3小时前")

    def test_yesterday(self):
        self.assertEqual(self.rel(30 * 3600), "昨天")

    def test_days(self):
        self.assertEqual(self.rel(3 * 86400), "3天前")

    def test_older_falls_back_to_date(self):
        # 10 days ago -> a MM-DD calendar date, not "10天前"
        out = self.rel(10 * 86400)
        self.assertNotIn("天前", out)
        self.assertRegex(out, r"^\d{2}-\d{2}$")


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = write_session(self.tmp, SID, FIXTURE_LINES)
        self.rec = recall.extract(self.path)

    def test_session_id_from_filename(self):
        self.assertEqual(self.rec["session_id"], SID)

    def test_cwd(self):
        self.assertEqual(self.rec["cwd"], "/repo/path")

    def test_ai_title(self):
        self.assertEqual(self.rec["ai_title"], "Review requirement")

    def test_prompts_filter_junk_keep_order(self):
        self.assertEqual(self.rec["prompts"],
                         ["看下这个需求 你规划一下", "帮我部署 prod"])

    def test_files_changed_unique_in_order(self):
        self.assertEqual(self.rec["files_changed"],
                         ["/repo/path/scheduler.go", "/repo/path/schedule_test.go"])

    def test_last_assistant_text(self):
        self.assertIn("已触发部署", self.rec["last_assistant"])

    def test_message_count(self):
        self.assertEqual(self.rec["msg_count"], 5)

    def test_has_mtime(self):
        self.assertGreater(self.rec["mtime"], 0)

    def test_projects_single_repo_with_branch_breakdown(self):
        projs = self.rec["projects"]
        self.assertEqual(len(projs), 1)
        self.assertEqual(projs[0]["name"], "path")  # _repo_root('/repo/path')
        names = {b["name"] for b in projs[0]["branches"]}
        self.assertEqual(names, {"master", "release-test"})
        # master has more messages -> sorted first
        self.assertEqual(projs[0]["branches"][0]["name"], "master")


class ExtractDedupTest(unittest.TestCase):
    def test_consecutive_duplicate_prompts_collapse(self):
        tmp = tempfile.mkdtemp()
        lines = [
            {"type": "last-prompt", "lastPrompt": "改一下这个"},
            {"type": "last-prompt", "lastPrompt": "改一下这个"},
            {"type": "last-prompt", "lastPrompt": "改一下这个"},
            {"type": "last-prompt", "lastPrompt": "再改另一个"},
            {"type": "last-prompt", "lastPrompt": "改一下这个"},  # non-consecutive, kept
        ]
        rec = recall.extract(write_session(tmp, SID, lines))
        self.assertEqual(rec["prompts"], ["改一下这个", "再改另一个", "改一下这个"])


class ExtractEdgeTest(unittest.TestCase):
    def test_empty_file_returns_none(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "empty.jsonl")
        open(path, "w").close()
        self.assertIsNone(recall.extract(path))

    def test_corrupt_lines_are_skipped(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, SID + ".jsonl")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"type": "last-prompt",
                                "lastPrompt": "real prompt here"}) + "\n")
        rec = recall.extract(path)
        self.assertEqual(rec["prompts"], ["real prompt here"])


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, "a.jsonl")
        self.b = os.path.join(self.tmp, "b.jsonl")
        for p in (self.a, self.b):
            open(p, "w").close()
        os.utime(self.a, (100, 100))   # older
        os.utime(self.b, (200, 200))   # newer
        self.calls = []

    def fake_extract(self, path):
        self.calls.append(path)
        return {"session_id": os.path.basename(path), "mtime": os.path.getmtime(path)}

    def test_extracts_all_and_sorts_newest_first(self):
        recs, _ = recall.build_index([self.a, self.b], {}, self.fake_extract)
        self.assertEqual([r["session_id"] for r in recs], ["b.jsonl", "a.jsonl"])
        self.assertCountEqual(self.calls, [self.a, self.b])

    def test_reuses_cache_when_mtime_unchanged(self):
        _, cache = recall.build_index([self.a, self.b], {}, self.fake_extract)
        self.calls.clear()
        recs, _ = recall.build_index([self.a, self.b], cache, self.fake_extract)
        self.assertEqual(self.calls, [])  # nothing re-extracted
        self.assertEqual(len(recs), 2)

    def test_reextracts_when_mtime_changes(self):
        _, cache = recall.build_index([self.a, self.b], {}, self.fake_extract)
        self.calls.clear()
        os.utime(self.a, (300, 300))  # a changed
        recs, _ = recall.build_index([self.a, self.b], cache, self.fake_extract)
        self.assertEqual(self.calls, [self.a])
        self.assertEqual(recs[0]["session_id"], "a.jsonl")  # now newest

    def test_prunes_entries_for_missing_files(self):
        cache = {"/gone/x.jsonl": {"mtime": 1, "record": {"session_id": "x"}}}
        _, new_cache = recall.build_index([self.a], cache, self.fake_extract)
        self.assertNotIn("/gone/x.jsonl", new_cache)
        self.assertIn(self.a, new_cache)


def sample_record(**over):
    rec = {
        "session_id": SID,
        "path": "/x/" + SID + ".jsonl",
        "cwd": "/Users/me/go/src/github.com/theplant/mcd-website",
        "projects": [{"name": "mcd-website", "path": "/p", "count": 64,
                      "branches": [{"name": "release-test", "count": 64}]}],
        "ai_title": "Review requirement",
        "prompts": ["看下这个需求", "填了日本时间显示0时区", "帮我部署 prod"],
        "files_changed": ["/repo/scheduler.go", "/repo/sub/schedule_test.go"],
        "last_assistant": "已触发 prod 部署，等 CI…",
        "mtime": 999_000,
        "msg_count": 64,
    }
    rec.update(over)
    return rec


class TruncateColsTest(unittest.TestCase):
    def test_short_string_unchanged(self):
        self.assertEqual(recall._truncate_cols("abcdef", 100), "abcdef")

    def test_collapses_whitespace(self):
        self.assertEqual(recall._truncate_cols("a\tb\nc", 100), "a b c")

    def test_ascii_truncated_with_ellipsis(self):
        out = recall._truncate_cols("abcdefghij", 5)
        self.assertEqual(out, "abcd…")  # 4 cols + ellipsis = 5

    def test_cjk_counts_as_two_columns(self):
        # cols=6 -> room for 2 wide chars (4 cols) then ellipsis
        self.assertEqual(recall._truncate_cols("你好世界很长", 6), "你好…")


class PadTest(unittest.TestCase):
    def test_pads_ascii_to_display_width(self):
        self.assertEqual(recall._pad("ab", 5), "ab   ")

    def test_counts_cjk_as_two_columns(self):
        # "刚刚" = 4 display cols -> 1 trailing space to reach 5
        self.assertEqual(recall._pad("刚刚", 5), "刚刚 ")

    def test_truncates_when_too_long(self):
        out = recall._pad("abcdefgh", 5)
        self.assertEqual(sum(recall._char_cols(c) for c in out), 5)


class RenderTest(unittest.TestCase):
    NOW = 1_000_000

    def test_project_short_is_cwd_basename(self):
        self.assertEqual(recall.project_short("/a/b/mcd-website"), "mcd-website")
        self.assertEqual(recall.project_short("/a/b/mcd-website/"), "mcd-website")

    def test_last_prompt_display_uses_last_prompt(self):
        self.assertEqual(recall.last_prompt_display(sample_record()), "帮我部署 prod")

    def test_last_prompt_display_falls_back_to_title(self):
        rec = sample_record(prompts=[])
        self.assertEqual(recall.last_prompt_display(rec), "Review requirement")

    def test_fzf_line_recovers_id_and_cwd_via_parse_selection(self):
        line = recall.fzf_line(sample_record(), self.NOW)
        sid, cwd = recall.parse_selection(line)
        self.assertEqual(sid, SID)
        self.assertEqual(cwd, "/Users/me/go/src/github.com/theplant/mcd-website")

    def test_fzf_line_keeps_id_and_cwd_in_unsearched_tail_fields(self):
        # fields 3,4 carry id/cwd; --with-nth=1,2 keeps them off display/search
        fields = recall.fzf_line(sample_record(), self.NOW).split("\t")
        self.assertEqual(len(fields), 4)
        self.assertEqual(fields[2], SID)
        self.assertEqual(fields[3], "/Users/me/go/src/github.com/theplant/mcd-website")

    def test_fzf_line_visible_columns_are_padded_for_alignment(self):
        fields = recall.fzf_line(sample_record(), self.NOW).split("\t")
        # reltime padded to a fixed display width before the project column
        self.assertTrue(fields[0].startswith(recall._pad(
            recall.relative_time(999_000, self.NOW), recall._TIME_COL)))
        self.assertIn("mcd-website", fields[0])
        self.assertIn("帮我部署 prod", fields[0])  # last prompt lives in the visible col

    def test_fzf_line_search_field_contains_whole_trail(self):
        searchable = " ".join(recall.fzf_line(sample_record(), self.NOW).split("\t")[:2])
        self.assertIn("时区", searchable)
        self.assertIn("帮我部署", searchable)

    def test_fzf_line_search_includes_branch_and_title_not_redundant_project(self):
        rec = sample_record(
            ai_title="Add parent nutrition field",
            projects=[{"name": "mcd-website", "path": "/p", "count": 9,
                       "branches": [{"name": "mdx-12752-add-parent-nutrition-field",
                                     "count": 9}]}])
        fields = recall.fzf_line(rec, self.NOW).split("\t")
        searchable = " ".join(fields[:2])
        self.assertIn("nutrition", searchable)          # branch name searchable
        self.assertIn("Add parent nutrition", searchable)  # ai title searchable
        self.assertNotIn("mcd-website", fields[1])      # not duplicated into keyword tail
        self.assertIn("mcd-website", fields[0])         # already in the visible column

    def test_fzf_line_sanitizes_tabs_and_newlines(self):
        rec = sample_record(prompts=["line1\twith\ttabs\nand newline"])
        line = recall.fzf_line(rec, self.NOW)
        self.assertEqual(len(line.split("\t")), 4)  # field count intact
        self.assertNotIn("\n", line)

    def test_preview_contains_key_sections(self):
        out = recall.preview_text(sample_record(), self.NOW)
        self.assertIn("mcd-website", out)
        self.assertIn("release-test", out)            # branch shown
        self.assertIn("Review requirement", out)      # ai title
        self.assertIn("帮我部署 prod", out)             # trail
        self.assertIn("已触发 prod 部署", out)          # last assistant
        self.assertIn("scheduler.go", out)            # file basename
        self.assertIn("schedule_test.go", out)

    def test_preview_dedupes_repeated_basenames(self):
        rec = sample_record(files_changed=["/a/x.go", "/b/x.go", "/a/y.go"])
        out = recall.preview_text(rec, self.NOW)
        self.assertEqual(out.count("x.go"), 1)
        self.assertIn("y.go", out)

    def test_preview_files_listed_one_per_line(self):
        rec = sample_record(files_changed=["/a/x.go", "/a/y.go"])
        out = recall.preview_text(rec, self.NOW)
        file_lines = [l for l in out.splitlines() if l.lstrip().startswith("·")
                      and (".go" in l)]
        self.assertEqual(len(file_lines), 2)

    def test_preview_truncates_long_trail_item_to_one_line(self):
        rec = sample_record(prompts=["这是一个非常长的提示句子" * 20])
        out = recall.preview_text(rec, self.NOW)
        trail = [l for l in out.splitlines() if l.startswith(("·", "▶"))]
        self.assertEqual(len(trail), 1)
        self.assertTrue(trail[0].endswith("…"))

    def test_preview_single_project_lists_branches_vertically_starring_top(self):
        rec = sample_record(projects=[{"name": "p", "path": "/p", "count": 33,
                                       "branches": [{"name": "ci/x", "count": 30},
                                                    {"name": "master", "count": 3}]}])
        out = recall.preview_text(rec, self.NOW)
        branch_lines = [l for l in out.splitlines()
                        if l.lstrip().startswith(("★", "·")) and
                        ("master" in l or "ci/x" in l)]
        self.assertEqual(len(branch_lines), 2)          # one per line (vertical)
        star_line = next(l for l in out.splitlines() if l.lstrip().startswith("★"))
        self.assertIn("ci/x", star_line)                # most messages -> starred
        self.assertNotIn("master", star_line)
        self.assertIn("30", star_line)                  # count shown

    def test_preview_single_branch_no_star(self):
        rec = sample_record(projects=[{"name": "p", "path": "/p", "count": 9,
                                       "branches": [{"name": "main", "count": 9}]}])
        out = recall.preview_text(rec, self.NOW)
        self.assertIn("main", out)
        self.assertNotIn("★", out)

    def test_preview_multi_project_tree_sorted_with_stars(self):
        rec = sample_record(projects=[
            {"name": "proctor", "path": "/w/proctor", "count": 4534,
             "branches": [{"name": "main", "count": 4000}, {"name": "fix", "count": 534}]},
            {"name": "qor_demo", "path": "/w/qor_demo", "count": 854,
             "branches": [{"name": "master", "count": 854}]},
        ])
        out = recall.preview_text(rec, self.NOW)
        self.assertIn("项目/分支", out)
        lines = out.splitlines()
        proctor_i = next(i for i, l in enumerate(lines) if "proctor" in l)
        qor_i = next(i for i, l in enumerate(lines) if "qor_demo" in l)
        self.assertLess(proctor_i, qor_i)               # bigger project on top
        self.assertTrue(lines[proctor_i].startswith("★"))   # top project starred
        self.assertTrue(lines[qor_i].startswith("·"))
        # branches nested (indented) under their project
        main_line = next(l for l in lines if "main" in l and "4000" in l)
        self.assertTrue(main_line.startswith("    "))

    def test_preview_caps_long_trail(self):
        rec = sample_record(prompts=[f"p{i}" for i in range(20)])
        out = recall.preview_text(rec, self.NOW)
        self.assertIn("p19", out)       # newest kept
        self.assertNotIn("p0 ", out)    # oldest dropped
        self.assertIn("更早", out)       # overflow marker


class CacheIOTest(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(recall.cache_load("/no/such/cache.json"), {})

    def test_corrupt_file_is_empty(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "c.json")
        with open(p, "w") as f:
            f.write("{not json")
        self.assertEqual(recall.cache_load(p), {})

    def test_round_trip(self):
        tmp = tempfile.mkdtemp()
        p = os.path.join(tmp, "c.json")
        cache = {"/a.jsonl": {"mtime": 1.0, "record": {"session_id": "a"}}}
        recall.cache_save(p, cache)
        self.assertEqual(recall.cache_load(p), cache)

    def test_load_ignores_mismatched_or_missing_version(self):
        tmp = tempfile.mkdtemp()
        # legacy flat format (no version) -> invalidated
        legacy = os.path.join(tmp, "legacy.json")
        with open(legacy, "w") as f:
            json.dump({"/a.jsonl": {"mtime": 1.0, "record": {}}}, f)
        self.assertEqual(recall.cache_load(legacy), {})
        # explicit older version -> invalidated
        old = os.path.join(tmp, "old.json")
        with open(old, "w") as f:
            json.dump({"version": recall.CACHE_VERSION - 1, "entries": {"/a": {}}}, f)
        self.assertEqual(recall.cache_load(old), {})


class FilterHereTest(unittest.TestCase):
    def test_keeps_only_sessions_under_cwd(self):
        here = sample_record(cwd="/repo/app")
        sub = sample_record(cwd="/repo/app/worktree-x")
        other = sample_record(cwd="/repo/other")
        kept = recall.filter_here([here, sub, other], "/repo/app")
        self.assertIn(here, kept)
        self.assertIn(sub, kept)
        self.assertNotIn(other, kept)


class ParseSelectionTest(unittest.TestCase):
    NOW = 1_000_000

    def test_roundtrips_session_id_and_cwd_from_fzf_line(self):
        rec = sample_record()
        line = recall.fzf_line(rec, self.NOW)
        sid, cwd = recall.parse_selection(line)
        self.assertEqual(sid, rec["session_id"])
        self.assertEqual(cwd, rec["cwd"])

    def test_blank_selection_returns_none(self):
        self.assertEqual(recall.parse_selection(""), (None, None))


class ExitRowTest(unittest.TestCase):
    def test_exit_line_parses_to_exit_sentinel(self):
        sid, _ = recall.parse_selection(recall._exit_line())
        self.assertEqual(sid, recall.EXIT_ID)

    def test_exit_line_is_searchable_by_exit_and_quit(self):
        line = recall._exit_line()
        for kw in ("exit", "quit", "退出"):
            self.assertIn(kw, line)

    def test_exit_sentinel_is_not_a_real_session_id(self):
        # so a chosen exit row never matches a real record
        self.assertTrue(recall.EXIT_ID.startswith("__"))


class RunListTest(unittest.TestCase):
    def test_emits_copyable_resume_command_per_record(self):
        out = io.StringIO()
        recall.run_list([sample_record()], 1_000_000, out)
        text = out.getvalue()
        self.assertIn("claude -r " + SID, text)
        self.assertIn("mcd-website", text)
        self.assertIn("帮我部署 prod", text)

    def test_empty_records_prints_friendly_message(self):
        out = io.StringIO()
        recall.run_list([], 1_000_000, out)
        self.assertIn("没有", out.getvalue())


if __name__ == "__main__":
    unittest.main()
