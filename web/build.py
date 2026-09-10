"""Assemble the single-file browser app.

pdf.js, its worker and ExcelJS are inlined so the page has zero network
dependencies - it runs from a local file, offline, forever.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(lib_dir: Path, out_path: Path) -> Path:
    parts = {
        "/*__PDFJS__*/": lib_dir / "build" / "pdf.min.mjs",
        "/*__PDF_WORKER__*/": lib_dir / "build" / "pdf.worker.min.mjs",
        "/*__EXCELJS__*/": lib_dir / "dist" / "exceljs.min.js",
        "/*__APP__*/": HERE / "app.js",
    }
    html = (HERE / "shell.html").read_text(encoding="utf-8")

    for token, path in parts.items():
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        source = path.read_text(encoding="utf-8")
        if "</script" in source:
            raise SystemExit(f"{path} contains </script and cannot be inlined verbatim")
        # pdf.js exports via ESM as `export{a as Foo, b as Bar}`. Rewrite that
        # into a plain object so the bundle can be concatenated into the module
        # script and reached as `pdfjsLib`.
        if token == "/*__PDFJS__*/":
            source, count = re.subn(
                r"export\{([^}]*)\}",
                lambda m: "const pdfjsLib={"
                + re.sub(r"([A-Za-z0-9_$]+)\s+as\s+([A-Za-z0-9_$]+)", r"\2:\1", m.group(1))
                + "};",
                source,
            )
            if count != 1:
                raise SystemExit(f"expected one export block in pdf.js, found {count}")
        html = html.replace(token, source)

    out_path.write_text(html, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    lib = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "lib" / "package"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "mcb-statement-to-excel.html"
    print(build(lib, out))
