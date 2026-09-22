"""S1 改动清单的单测: 一次调用改了哪些文件 (真值 / 推断) + 每个任务的清单 + 「验证了没」的口径。

盯死的护栏:
- Edit / Write 的行数按请求本身算 (真值); Write 覆盖前多大**不知道** -> del 为 None, 不写 0;
- Bash 是推断: 认得出文件就记文件, 认不出 (内联脚本 / git reset --hard / 通配符 / 整棵树) 就记「说不清」, 绝不瞎猜文件名;
- `git add` / `git commit` 不动文件内容 -> 不算改动 (否则每次提交都会冒出一条假改动);
- 「验证」给的是「最后一次验证是什么 + 它之后又改了多少」, 不是一句「没验证」;
- 路径按任务的工作目录归一; Claude 自己的临时脚本目录不算「界外」。
"""

import json

import pytest

from tokmon import parser, serve, trace
from test_trace import SID, asst, human, iso, result, tool_use, usage, write_jsonl

CWD = "C:\\Users\\u\\.vscode\\demo"


# ------------------------------------------------------------------ 一次调用改了什么 (纯函数)

def test_edit_and_write_are_true_values():
    e = trace.file_changes("Edit", {"file_path": "a.py", "old_string": "x\ny", "new_string": "1\n2\n3"})
    assert e["src"] == "true" and e["items"] == [{"path": "a.py", "op": "edit", "add": 3, "del": 2, "approx": False}]
    w = trace.file_changes("Write", {"file_path": "b.md", "content": "a\nb"})
    assert w["items"][0] == {"path": "b.md", "op": "write", "add": 2, "del": None}   # 覆盖前多大不知道 -> None
    m = trace.file_changes("MultiEdit", {"file_path": "c.py", "edits": [
        {"old_string": "a", "new_string": "1\n2"}, {"old_string": "b\nc", "new_string": "3", "replace_all": True}]})
    assert m["items"][0]["add"] == 3 and m["items"][0]["del"] == 3 and m["items"][0]["approx"] is True
    nb = trace.file_changes("NotebookEdit", {"notebook_path": "n.ipynb", "edit_mode": "delete"})
    assert nb["items"][0]["op"] == "delete" and nb["items"][0]["add"] is None
    assert trace.file_changes("Read", {"file_path": "a.py"}) is None
    assert trace.file_changes("Edit", {"old_string": "x", "new_string": "y"}) is None       # 没有路径就不记


@pytest.mark.parametrize("cmd,paths,ops,unknown", [
    ("sed -i 's/a/b/' tokmon/trace.py", ["tokmon/trace.py"], ["edit"], False),
    ("perl -pi -e s/a/b/ x.txt", ["x.txt"], ["edit"], False),
    ("cat x.txt > out.log", ["out.log"], ["write"], False),
    ("echo hi >> notes.md", ["notes.md"], ["write"], False),
    ("cat a | tee b.txt", ["b.txt"], ["write"], False),
    ("mv a.py b.py", ["a.py", "b.py"], ["move", "move"], False),
    ("cp src.py dst.py", ["dst.py"], ["write"], False),
    ("rm -rf build", ["build"], ["delete"], False),
    ("touch new.txt", ["new.txt"], ["write"], False),
    ("git checkout -- tokmon/trace.py", ["tokmon/trace.py"], ["revert"], False),
    ("Set-Content -Path a.txt -Value hello", ["a.txt"], ["write"], False),
    ("Remove-Item old.log", ["old.log"], ["delete"], False),
    ("git checkout -- .", [], [], True),                      # 整棵树: 不猜是哪些文件
    ("sed -i 's/a/b/' *.py", [], [], True),                   # 通配符: 展开不出来
    ("git reset --hard", [], [], True),
    ("git stash pop", [], [], True),
    ("unzip pkg.zip", [], [], True),
    ("python - <<'EOF'\nopen('x','w')\nEOF", [], [], True),   # 内联脚本: 可能写文件, 不知道是哪个
])
def test_bash_changes_are_inferred(cmd, paths, ops, unknown):
    got = trace.file_changes("Bash", {"command": cmd}, "修改")
    assert got is not None and got["src"] == "inferred"
    assert [i["path"] for i in got["items"]] == paths
    assert [i["op"] for i in got["items"]] == ops
    assert got["unknown"] is unknown
    assert all(i["add"] is None and i["del"] is None for i in got["items"])   # 命令改的不给行数


@pytest.mark.parametrize("cmd,stage", [
    ("git commit -m x", "修改"),          # 提交不动文件内容
    ("git add -A", "修改"),
    ("mkdir -p build", "修改"),
    ("chmod +x run.sh", "修改"),
    ("cat x.txt > /dev/null", "修改"),    # 丢进黑洞不算改文件
    ("npm test", "验证"),
    ("python patch.py", "运行"),          # 看不出在改 -> 不猜
])
def test_commands_that_are_not_file_changes(cmd, stage):
    assert trace.file_changes("Bash", {"command": cmd}, stage) is None


def test_unrecognized_edit_stage_is_honest():
    """看得出这一步在「修改」, 但命令没看懂 -> 记一笔「改了, 不知道改了哪个」, 不假装没改。"""
    got = trace.file_changes("Bash", {"command": "make install"}, "修改")
    assert got["unknown"] is True and got["items"] == []


def test_norm_path_and_outside():
    roots = ["c:/users/u/proj"]
    assert trace._norm_path("C:\\Users\\u\\proj\\tokmon\\trace.py", roots) == ("tokmon/trace.py", False)
    assert trace._norm_path("./a.py", roots) == ("a.py", False)                      # 相对路径: 就在工作目录下
    assert trace._norm_path("C:/Users/u/other/x.py", roots) == ("C:/Users/u/other/x.py", True)
    scratch = "C:/Users/u/AppData/Local/Temp/claude/s/scratchpad/p.py"
    assert trace._norm_path(scratch, roots) == ("临时脚本/p.py", False)               # Claude 的临时脚本: 不算界外, 只留尾巴


# ------------------------------------------------------------------ 清单组装

def _ch(i, t, path, src="true", op="edit", add=None, dele=None, unknown=False):
    items = [] if path is None else [{"path": path, "op": op, "add": add, "del": dele}]
    return {"id": f"c{i}", "t": t, "src": src, "unknown": unknown, "items": items}


def _call(cid, t, stage, fail=False):
    return {"id": cid, "t0": t, "meta": {"stage": stage}, "flags": ["fail"] if fail else [], "label": "npm test"}


def test_ledger_merges_files_and_keeps_calls():
    L = trace.build_ledger([_ch(1, 10, "a.py", add=3, dele=1), _ch(2, 20, "a.py", src="inferred", op="revert"),
                            _ch(3, 30, "b.md", op="write", add=5), _ch(4, 40, None, src="inferred", unknown=True)],
                           [], {"c:/proj"})
    files = {f["path"]: f for f in L["files"]}
    a = files["a.py"]
    assert a["n"] == 2 and a["add"] == 3 and a["del"] == 1 and a["src"] == "both"
    assert a["ops"] == {"edit": 1, "revert": 1} and a["calls"] == ["c1", "c2"] and (a["t0"], a["t1"]) == (10, 20)
    assert files["b.md"]["del"] is None                        # 只写了不知道删了多少 -> None, 不是 0
    assert a["add_partial"] is True and a["del_partial"] is True     # 还原那次算不出行数 -> 3 只是下限
    assert L["totals"] == {"files": 2, "changes": 3, "unknown": 1, "add": 8, "del": 1, "add_partial": True,
                           "del_partial": True, "inferred": 1, "outside": 0}
    assert [u["node"] for u in L["unknown_calls"]] == ["c4"]
    assert L["files"][0]["path"] == "a.py"                     # 按改动次数排
    assert trace.build_ledger([], [], set()) is None


def test_ledger_verify_is_last_verification_and_what_came_after():
    changes = [_ch(1, 10, "a.py"), _ch(2, 30, "docs.md", op="write", add=2)]
    calls = [_call("v1", 20, "验证"), _call("v2", 25, "验证", fail=True), _call("x", 28, "运行")]
    L = trace.build_ledger(changes, calls, set())
    assert L["verify"]["node"] == "v2" and L["verify"]["ok"] is False      # 最后一次验证, 失败了
    assert L["after_verify"] == {"changes": 1, "files": 1}                 # 验证之后又改了 docs.md
    clean = trace.build_ledger([_ch(1, 10, "a.py")], [_call("v1", 20, "验证")], set())
    assert clean["verify"]["ok"] is True and clean["after_verify"]["changes"] == 0
    none = trace.build_ledger([_ch(1, 10, "a.py")], [_call("x", 20, "运行")], set())
    assert none["verify"] is None and none["after_verify"] == {"changes": 1, "files": 1}


# ------------------------------------------------------------------ 端到端: 合成会话

SECRET = "CHG-SECRET-9876543210"


@pytest.fixture
def chg(tmp_path, monkeypatch):
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_AGENT_IDX", {}), ("_SCRIPTS", {}), ("_META", {}),
                      ("_BUILT", {}), ("_CLAIMED", {}), ("_STATS", {}), ("_STAMPS", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    monkeypatch.setattr(serve, "_wf_running", lambda base: set())
    monkeypatch.setattr(serve, "_wf_known_secrets", lambda: (SECRET,))
    base = tmp_path / "projects"
    write_jsonl(base / "c--Users-u--vscode-demo" / f"{SID}.jsonl", [
        human(0, "P1", "修一下点击失效"),
        asst(1, "m1", "r1", tool_use("e1", "Edit", {"file_path": CWD + "\\pkg\\mod.py", "old_string": "x\ny",
                                                    "new_string": "1\n2\n3"}), usage(out=5)),
        result(2, "e1", "ok"),
        asst(3, "m2", "r2", tool_use("b1", "Bash", {"command": "sed -i 's/a/b/' pkg/mod.py"}), usage(out=5)),
        result(4, "b1", "ok"),
        asst(5, "m3", "r3", tool_use("t1", "Bash", {"command": "pytest -q"}), usage(out=5)),
        result(6, "t1", "1 passed"),
        asst(7, "m4", "r4", tool_use("w1", "Write", {"file_path": f"docs/{SECRET}.md", "content": "a\nb\nc"}), usage(out=5)),
        result(8, "w1", "ok"),
        asst(9, "m5", "r5", tool_use("b2", "Bash", {"command": "git reset --hard"}), usage(out=5)),
        result(10, "b2", "HEAD is now at x"),
        asst(11, "m6", "r6", {"type": "text", "text": "好了"}, usage(out=5)),
        human(200, "P2", "只看看"),
        asst(201, "m7", "r7", tool_use("r1", "Read", {"file_path": "a.py"}), usage(out=5)),
        result(202, "r1", "x"),
        asst(203, "m8", "r8", {"type": "text", "text": "看过了"}, usage(out=5)),
    ])
    return base


def test_task_ledger_end_to_end(chg):
    built = {r["id"]: trace.get_task(r["id"], chg) for r in trace.list_tasks(chg)}
    L = built["P1"]["summary"]["changes"]
    files = {f["path"]: f for f in L["files"]}
    assert set(files) == {"pkg/mod.py", f"docs/{SECRET}.md"}
    assert files["pkg/mod.py"]["n"] == 2 and files["pkg/mod.py"]["src"] == "both"      # 绝对路径归一到相对
    assert files["pkg/mod.py"]["add"] == 3 and files["pkg/mod.py"]["del"] == 2
    assert L["totals"]["unknown"] == 1 and [u["node"] for u in L["unknown_calls"]] == ["b2"]
    assert L["verify"]["node"] == "t1" and L["verify"]["ok"] is True
    assert L["after_verify"] == {"changes": 2, "files": 1}                             # 验证后: Write + git reset
    assert built["P2"]["summary"]["changes"] is None                                   # 只读的任务没有清单
    tree_ids = {n["id"] for n in trace.walk([built["P1"]["tree"]])}
    for f in L["files"]:                                                               # 每条都能跳回回放
        assert set(f["calls"]) <= tree_ids


def test_list_rows_carry_files_for_search(chg):
    rows = {r["id"]: r for r in trace.list_tasks(chg)}
    assert dict(rows["P1"]["files"])["pkg/mod.py"] == 2 and rows["P1"]["chg"]["files"] == 2
    assert rows["P1"]["chg"]["verified"] is True and rows["P1"]["chg"]["after_verify"] == 2
    assert rows["P2"]["files"] == [] and rows["P2"]["chg"] is None


def test_stats_change_numbers_match_their_detail(chg):
    res = trace.stats(chg)
    c, st = res["changes"], res["stamp"]
    assert c["tasks"] == 1 and c["files"] == 2 and c["unknown"] == 1
    assert c["changes"] == 4 and c["inferred"] == 2                    # 4 次调用改了文件, 其中 2 次是命令 (含说不清的那次)
    assert (c["verified_clean"], c["changed_after"], c["never_verified"]) == (0, 1, 0)
    for n, ref in ((c["tasks"], "chg-tasks"), (c["changes"], "chg-calls"), (c["inferred"], "chg-inferred"),
                   (c["unknown"], "chg-unknown"), (c["changed_after"], "chg-after"),
                   (c["never_verified"], "chg-noverify")):
        assert trace.stats_refs(st, ref, limit=500)["total"] == n


def test_serve_redacts_paths(chg):
    blob = json.dumps(serve._wf_tasks(chg, {"since": ["all"]}), ensure_ascii=False)
    assert SECRET not in blob and "***" in blob
    task = serve._wf_task(chg, {"id": ["P1"]})
    assert SECRET not in json.dumps(task, ensure_ascii=False)
    assert task["summary"]["changes"]["totals"]["files"] == 2


# ------------------------------------------------------------------ 实际落盘的补丁 (真值) 优先于按请求算

def test_patch_stats_counts_applied_diff():
    tur = {"filePath": "C:/p/a.py", "userModified": True, "structuredPatch": [
        {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
         "lines": [" keep", "-gone", "+new1", "+new2"]},
        {"lines": ["-x", "+y"]}]}
    assert trace.patch_stats(tur) == {"path": "C:/p/a.py", "add": 3, "del": 2, "user_modified": True}
    assert trace.patch_stats({"structuredPatch": []}) is None and trace.patch_stats({}) is None


def test_applied_patch_beats_request_side_count(tmp_path, monkeypatch):
    """Edit 的返回里带实际补丁时, 行数用补丁的 (Write 覆盖掉多少行也只有补丁知道)。"""
    for name, val in (("_FILES", {}), ("_TASK_INDEX", {}), ("_BUILT", {}), ("_CLAIMED", {}), ("_AGENT_IDX", {})):
        monkeypatch.setattr(trace, name, val)
    monkeypatch.setattr(trace, "_BASELINE", {"t": 0.0, "data": None})
    monkeypatch.setattr(parser, "_file_cache", {})
    base = tmp_path / "projects"
    write_jsonl(base / "c--Users-u--vscode-demo" / f"{SID}.jsonl", [
        human(0, "P1", "改一下"),
        asst(1, "m1", "r1", tool_use("w1", "Write", {"file_path": "big.py", "content": "a\nb"}), usage(out=5)),
        dict(result(2, "w1", "ok"), toolUseResult={"filePath": "big.py", "structuredPatch": [
            {"lines": ["-old1", "-old2", "-old3", "+a", "+b"]}]}),
        asst(3, "m2", "r2", tool_use("e1", "Edit", {"file_path": "x.py", "old_string": "a", "new_string": "b",
                                                    "replace_all": True}), usage(out=5)),
        result(4, "e1", "ok"),
        asst(5, "m3", "r3", {"type": "text", "text": "好"}, usage(out=5)),
    ])
    L = trace.get_task("P1", base)["summary"]["changes"]
    files = {f["path"]: f for f in L["files"]}
    assert files["big.py"]["add"] == 2 and files["big.py"]["del"] == 3      # 覆盖掉的 3 行来自实际补丁
    assert files["big.py"]["add_partial"] is False
    assert files["x.py"]["approx"] is True and files["x.py"]["add_partial"] is True   # replace_all: 只是下限
