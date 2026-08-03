"""构建 docs/atlas.html —— 把 vendor/mermaid.min.js 内联进 atlas.src.html。

产出一个真正自包含、离线可用的单一 HTML（符合项目「本地优先」气质）。
改内容只动 atlas.src.html，然后重跑本脚本即可：  python docs/build_atlas.py
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARKER = "/*__MERMAID_LIB__*/"


def main() -> None:
    src = (HERE / "atlas.src.html").read_text(encoding="utf-8")
    lib = (HERE / "vendor" / "mermaid.min.js").read_text(encoding="utf-8")
    if MARKER not in src:
        raise SystemExit(f"placeholder {MARKER!r} not found in atlas.src.html")
    out = src.replace(MARKER, lib)
    dst = HERE / "atlas.html"
    dst.write_text(out, encoding="utf-8")
    print(f"built {dst}  ({len(out):,} bytes, mermaid inlined {len(lib):,} bytes)")


if __name__ == "__main__":
    main()
