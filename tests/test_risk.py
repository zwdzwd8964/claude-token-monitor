"""S2 风险标记的单测: 三类规则 + 命令解析的护栏 (含对抗式 review 确认的每一条复现)。

纪律 (MISSION_CONTROL 硬不变量「误报零容忍」): 宁可不标, 不要标错——
- 引号里的东西不算命令: `echo "rm -rf x"` 不是破坏性操作, `node -e "(a)=>{}"` 里的 `=>` 不是重定向;
- 内联脚本的正文不是 shell 代码, 但 heredoc 终止符之后的命令**要接着解析**;
- `#` 注释不是参数; `"$SP"/a.py` 是一个词, 变量展开不出来就记「说不清」, 绝不造出 `/a.py`;
- 预演 (`git clean -n` / `--dry-run` / `-WhatIf`) 一个字节都不删, 不算破坏性;
- 打转按**时间线**算, 而且失败要**冲着这个文件** (输出里点了它的名) 才算它的一轮。
"""

import json

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, asst, human, iso, result, tool_use, usage, write_jsonl


def _paths(cmd, stage="修改", name="Bash"):
    got = trace.file_changes(name, {"command": cmd}, stage)
    return None if got is None else ([i["path"] for i in got["items"]], got["unknown"])


# ------------------------------------------------------------------ 词法器

def test_shell_parse_quotes_do_not_split():
    P = trace.shell_parse("sed -i '/a;b/d' f.md && echo hi; ls")
    assert [[w[0] for w in c["words"]] for c in P["cmds"]] == [["sed", "-i", "/a;b/d", "f.md"], ["echo", "hi"], ["ls"]]
    assert [[w[0] for w in c["words"]] for c in trace.shell_parse('echo "x && y"')["cmds"]] == [["echo", "x && y"]]


def test_quoted_and_bare_parts_are_one_word():
    """review: `"$SP"/atk3.py` 曾被拆成 `$SP` 和 `/atk3.py`, 造出一个根目录下的假文件并标成「界外」。"""
    w = trace.shell_parse('rm -rf "$SP"/atk3.py')["cmds"][0]["words"][2]
    assert w == ("$SP/atk3.py", True)
    assert _paths('rm -rf "$SP"/dbdir-18464') == ([], True)                 # 展开不出来 -> 说不清, 不造文件名
    assert _paths('SP="C:/tmp/s"; rm -rf "$SP"/dbdir-18464') == (["C:/tmp/s/dbdir-18464"], False)   # 同一条命令里赋过值 -> 展开


def test_comments_are_not_arguments():
    assert _paths("rm -rf dist  # remove stale build") == (["dist"], False)
    P = trace.shell_parse("rm -rf build   # don't keep\npython x.py > out.txt")
    assert [[w[0] for w in c["words"]] for c in P["cmds"]] == [["rm", "-rf", "build"], ["python", "x.py"]]
    assert _paths("rm -rf build   # don't keep\npython x.py > out.txt") == (["build", "out.txt"], False)


def test_heredoc_body_skipped_but_commands_after_terminator_parsed():
    cmd = "cat > a.py <<'EOF'\ndef f(x) -> str:\n    rm -rf nothing\nEOF\nrm -rf build && sed -i \"s/a/b/\" tokmon/trace.py"
    assert _paths(cmd) == (["a.py", "build", "tokmon/trace.py"], False)     # 正文里的 rm 不算, 终止符之后的都算
    assert trace.destructive_kind("Bash", {"command": cmd}) == "rm -rf"
    script = "python - <<'EOF'\ndef f(x) -> str:\n    open('a.txt','w')\nEOF"
    assert _paths(script) == ([], True)                                    # 内联脚本: 可能写文件, 不知道是哪个
    assert trace.classify_call("Bash", {"command": script}) == "运行"
    assert trace.shell_parse("cat <<EOF\nnever closed")["unterminated"] is True
    assert _paths("cat > x.txt <<EOF\nnever closed") == (["x.txt"], True)   # 没闭合 -> 后面的不知道, 记说不清


def test_quoted_heredoc_marker_is_not_heredoc():
    """review: 引号里的 `<<` (位移运算) 曾被当成 heredoc, 一条纯读命令被记成改动, 还吞掉后面的 rm -rf。"""
    assert trace.file_changes("Bash", {"command": 'python -c "print(1 << 3)"'}, "运行") is None
    assert trace.destructive_kind("Bash", {"command": 'python -c "print(1 << 3)" && rm -rf build'}) == "rm -rf"


@pytest.mark.parametrize("cmd,want", [
    ("cat a > out.log", ["out.log"]),
    ("echo hi >> notes.md", ["notes.md"]),
    ('node -e "const f=(a)=>{return a>1}"', []),          # 箭头函数不是重定向
    ('echo "<div>x</div>"', []),                           # 标签不是重定向
    ("cmd 2>&1", []),
    ("test $((3>=2))", []),
    ("python x.py > /dev/null", ["/dev/null"]),
    ('echo hi > "my file.txt"', ["my file.txt"]),          # review: 引号里的空格不切断路径
])
def test_redirect_targets(cmd, want):
    assert trace.redirect_targets(cmd) == want


def test_variable_redirect_is_unknown_not_dropped():
    """review: `> "$LOG"` 曾被静默丢掉 (连「说不清」都没记)。"""
    assert _paths('echo ok > "$LOG"') == ([], True)
    assert _paths('SP="C:/tmp/s"\ncat > "$SP/probe.js" <<\'EOF\'\nx\nEOF') == (["C:/tmp/s/probe.js"], False)


@pytest.mark.parametrize("cmd,want", [
    ("sed -i '/^<a>$/d; /^<b>$/d' docs/R.md", ["docs/R.md"]),     # 引号里的分号不切段
    ("sed -i -e 's/a/b/' -e 's/c/d/' x.py", ["x.py"]),            # -e 后面是表达式
    ("sed -i '/INSERT/d' db.sql", ["db.sql"]),
    ('sed -i "s|old|new|g" tokmon/trace.py', ["tokmon/trace.py"]),  # review: 引号里的 | 不是管道
    ('sed -i "s/a/b => \\/Foo|bar/" real.js', ["real.js"]),
    ("perl -i -pe 's/a/b/' f.txt", ["f.txt"]),                    # -pe: 下一个词是脚本
])
def test_sed_paths_only_real_files(cmd, want):
    assert _paths(cmd)[0] == want


@pytest.mark.parametrize("cmd,name,want", [
    ("New-Item -ItemType Directory -Force C:\\out", "PowerShell", None),               # = mkdir, 不动文件内容
    ("Remove-Item -Path build -Recurse -Force -ErrorAction SilentlyContinue", "PowerShell", (["build"], False)),
    ('touch -d "2020-01-01" f.txt', "Bash", (["f.txt"], False)),                        # -d 的值不是文件
    ("Set-Content -Force out.txt -Value hi", "PowerShell", (["out.txt"], False)),
])
def test_flag_values_are_not_paths(cmd, name, want):
    assert _paths(cmd, name=name) == want


@pytest.mark.parametrize("cmd", ["git stash list", "git stash show -p", "git checkout -b feat", "git switch -c feat",
                                 "git apply --check p.diff", "git -C /repo status"])
def test_read_only_git_is_not_a_change(cmd):
    assert trace.file_changes("Bash", {"command": cmd}, "修改") is None


def test_non_file_targets_are_dropped():
    for cmd in ("curl -s x > /dev/null", "echo hi > /dev/stderr", "cat a > /dev/tcp/127.0.0.1/9420"):
        got = trace.file_changes("Bash", {"command": cmd}, "修改")
        assert got is None or got["items"] == []


# ------------------------------------------------------------------ 破坏性操作

@pytest.mark.parametrize("cmd,want", [
    ("rm -rf build", "rm -rf"),
    ("rm -fr /tmp/x", "rm -rf"),
    ("rm --recursive --force d", "rm -rf"),
    ("cd /x && rm -rf node_modules", "rm -rf"),
    ("rm -r build", None),                                   # 只有 -r 不算
    ("rm -f x.txt", None),
    ("rm notes.txt", None),
    ("git reset --hard HEAD~1", "git reset --hard"),
    ("git reset --soft HEAD~1", None),
    ("git clean -fd", "git clean"),
    ("git clean -n", None),                                  # 预演不算
    ("git clean -nd", None),                                 # review: 曾被 [A-Za-z]*[fdx] 吃掉 n 后命中 d
    ("git clean --dry-run", None),
    ("git clean -fdn", None),                                # -n 压过 -f
    ("git push --force origin main", "强推 (git push --force)"),
    ("git push --force --dry-run origin main", None),
    ("git push -f -n origin main", None),
    ("git push --force-with-lease", None),                   # 带 lease 的不算
    ("git push origin main", None),
    ("git checkout -- .", "整棵树还原 (git checkout/restore .)"),
    ("git restore .", "整棵树还原 (git checkout/restore .)"),
    ("git checkout -- a.py", None),
    ("Remove-Item -Recurse -Force build", "PowerShell 递归强删"),
    ("Remove-Item -Recurse -Force -WhatIf build", None),     # -WhatIf 只演示
    ('echo "rm -rf x"', None),                               # 引号里的不是命令
    ('psql -c "DROP TABLE users"', "删库 (DROP / TRUNCATE)"),
    ('python patch.py  # DROP TABLE 只是字符串', None),        # 不是数据库客户端 -> 不算
])
def test_destructive_kind(cmd, want):
    assert trace.destructive_kind("Bash", {"command": cmd}) == want


def test_destructive_mcp():
    assert trace.destructive_kind("mcp__railway__remove_service", {}) == "MCP 删除类调用 (remove_service)"
    assert trace.destructive_kind("mcp__railway__list_services", {}) is None


# ------------------------------------------------------------------ 连续失败 / 打转

def _c(cid, t, fail=False, stage="运行", tl="main"):
    return {"id": cid, "t0": t, "flags": ["fail"] if fail else [], "meta": {"stage": stage, "tl": tl}, "label": "x"}


def test_spikes_need_consecutive_failures_in_one_timeline():
    assert trace.SPIKE_FAILS == 3
    calls = [_c("a", 1, True), _c("b", 2, True), _c("c", 3, True), _c("d", 4, True), _c("e", 5)]
    assert trace._spikes(calls) == [["a", "b", "c", "d"]]
    broken = [_c("a", 1, True), _c("b", 2, True), _c("x", 2.5), _c("d", 4, True)]
    assert trace._spikes(broken) == []                       # 中间成功过 -> 断了 (严格相邻)
    split = [_c("a", 1, True), _c("b", 2, True, tl="w1"), _c("c", 3, True), _c("d", 4, True, tl="w1")]
    assert trace._spikes(split) == []                        # 两条时间线各 2 次, 不算一串


def _ch(cid, t, path, tl="main", op="edit"):
    return {"id": cid, "t": t, "tl": tl, "src": "true", "unknown": False,
            "items": [{"path": path, "op": op, "add": 1, "del": 1}]}


def test_thrash_counts_change_then_failed_verify_that_names_the_file():
    changes = [_ch(f"c{i}", i * 10, "src/a.py") for i in range(1, 5)]
    calls = [_c(f"v{i}", i * 10 + 5, True, "验证") for i in range(1, 4)]
    named = {f"v{i}": "FAILED tests/test_a.py::test_x\nsrc/a.py:3: AssertionError" for i in range(1, 4)}
    got = trace._thrash({}, changes, calls, [], named)
    assert got and got[0][0] == "src/a.py" and got[0][1] == 3
    assert trace._thrash({}, changes, [_c("v", 15, False, "验证")], [], named) == []        # 验证没失败 -> 不算


def test_thrash_does_not_blame_files_the_failure_never_names():
    """review: 只改了 CHANGELOG, 失败来自别处的测试 -> 不能判「CHANGELOG.md 改了 3 轮都没通过」。"""
    changes = [_ch(f"c{i}", i * 20, "CHANGELOG.md") for i in range(1, 4)]
    calls = [_c(f"v{i}", i * 20 + 5, True, "验证") for i in range(1, 4)]
    texts = {f"v{i}": "FAILED tests/test_core.py::test_parse - src/core.py:9: KeyError" for i in range(1, 4)}
    assert trace._thrash({}, changes, calls, [], texts) == []
    assert trace._thrash({}, changes, calls, [], {}) == []                                     # 没有失败正文 -> 不摊派
    blocked = {f"v{i}": "<tool_use_error>Blocked: CHANGELOG.md is outside the worktree" for i in range(1, 4)}
    assert trace._thrash({}, changes, calls, [], blocked) == []                                # 被工具拦下不是验证结果


def test_thrash_is_per_timeline():
    """同一个文件名, 两个 agent 各自在自己的 worktree 改 —— 不是一个人在来回。"""
    changes = [_ch(f"a{i}", i * 10, "x.py", tl="w1") for i in range(1, 4)] + \
              [_ch(f"b{i}", i * 10 + 1, "x.py", tl="w2") for i in range(1, 4)]
    calls = [_c(f"v{i}", i * 10 + 5, True, "验证", tl="w1") for i in range(1, 3)] + \
            [_c(f"u{i}", i * 10 + 6, True, "验证", tl="w2") for i in range(1, 3)]
    texts = {c["id"]: "x.py:1: error" for c in calls}
    assert trace._thrash({}, changes, calls, [], texts) == []  # 各自只有 2 轮, 不到 3 轮


# ------------------------------------------------------------------ 端到端

@pytest.fixture
def risky(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_STATS", {}), ("_STAMPS", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(serve, "_wf_running", lambda base: set())
    base = tmp_path / "projects"
    lines = [human(0, "P1", "改一下再清理")]
    t = 1
    for i in range(4):                                       # 改 a.py -> 测试失败, 来回 4 轮
        lines += [asst(t, f"m{i}a", f"r{i}a", tool_use(f"e{i}", "Edit", {"file_path": "a.py", "old_string": "x",
                                                                          "new_string": "y"}), usage(out=5)),
                  result(t + 1, f"e{i}", "ok"),
                  asst(t + 2, f"m{i}b", f"r{i}b", tool_use(f"t{i}", "Bash", {"command": "pytest -q"}), usage(out=5)),
                  result(t + 3, f"t{i}", "FAILED tests/test_a.py::test_x - a.py:3: AssertionError", err=True)]
        t += 4
    for i in range(4):                                       # 中间没有成功过的 4 次失败 = 连续失败
        lines += [asst(t, f"ms{i}", f"rs{i}", tool_use(f"s{i}", "Bash", {"command": f"curl -f http://x/{i}"}), usage(out=5)),
                  result(t + 1, f"s{i}", "connection refused", err=True)]
        t += 2
    lines += [asst(t, "mx", "rx", tool_use("rm1", "Bash", {"command": "rm -rf build"}), usage(out=5)),
              result(t + 1, "rm1", "ok"),
              asst(t + 2, "my", "ry", {"type": "text", "text": "好了"}, usage(out=5))]
    write_jsonl(base / "c--Users-u--vscode-demo" / f"{SID}.jsonl", lines)
    return base


def test_risks_end_to_end(risky):
    sm = trace.get_task("P1", risky)["summary"]
    rules = {r["rule"]: r for r in sm["risks"]}
    assert rules["destructive"]["label"].endswith("rm -rf") and rules["destructive"]["refs"][0]["id"] == "rm1"
    assert rules["thrash"]["n"] >= 3 and "a.py" in rules["thrash"]["label"]
    assert rules["error-spike"]["n"] == 5                    # 最后一次 pytest 失败 + 4 次 curl 失败, 中间没成功过
    assert all(r["level"] == "info" for r in sm["risks"])    # 审过精确率之前一律只做提示
    assert all(r.get("why") for r in sm["risks"])            # 每条都要能说清凭什么这么判
    tree_ids = {n["id"] for n in trace.walk([trace.get_task("P1", risky)["tree"]])}
    for r in sm["risks"]:
        for rf in r["refs"]:
            assert rf["kind"] != "call" or rf["id"] in tree_ids


def test_stats_risk_numbers_match_their_detail(risky):
    res = trace.stats(risky)
    st = res["stamp"]
    assert res["risks"]
    for r in res["risks"]:
        assert trace.stats_refs(st, r["ref_tasks"], limit=500)["total"] == r["tasks"]
        assert trace.stats_refs(st, r["ref_calls"], limit=500)["total"] == r["calls"]


def test_destructive_flag_lands_on_the_call(risky):
    calls = {n["id"]: n for n in trace.walk([trace.get_task("P1", risky)["tree"]]) if n["kind"] == "call"}
    assert "destructive" in calls["rm1"]["flags"] and calls["rm1"]["meta"]["destructive"] == "rm -rf"
    assert "destructive" not in calls["t0"]["flags"]
