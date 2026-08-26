"""Command line entry point: MCB statement PDF -> formatted Excel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .excel import write as write_excel
from .models import Statement
from .parse import dedupe, stitch
from .validate import Report, summarise, validate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcbx",
        description="Convert MCB Bank account statement PDFs into the required Excel format.",
    )
    parser.add_argument("pdfs", nargs="+", help="statement PDF(s); directories are scanned for *.pdf")
    parser.add_argument("-o", "--output", help="output .xlsx path (single input) or directory")
    parser.add_argument(
        "--engine",
        choices=("auto", "text", "ocr", "vision"),
        default="auto",
        help="auto (default): text layer if present, else Claude vision. "
             "'ocr' is offline Tesseract - free, but materially less accurate "
             "on scans (see README)",
    )
    parser.add_argument(
        "--wrap-join",
        choices=("none", "space"),
        default="none",
        help="how to rejoin cells wrapped over several printed lines (default: none)",
    )
    parser.add_argument(
        "--ocr-scale",
        type=float,
        default=4.0,
        help="ocr engine: page render multiplier (default: 4.0, ~300dpi)",
    )
    parser.add_argument(
        "--pages-per-call", type=int, default=4, help="vision engine: pages per request (default: 4)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=3, help="vision engine: parallel requests (default: 3)"
    )
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="high",
        help="vision engine: transcription effort (default: high)",
    )
    parser.add_argument("--json", dest="json_out", help="also write the extracted data as JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if validation finds errors (for unattended runs)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only report errors")
    parser.add_argument("--version", action="version", version=f"mcbx {__version__}")
    return parser


def convert(pdf_path: Path, args, log) -> tuple[Statement, Report]:
    """Run one PDF through extract -> stitch -> validate."""
    engine = args.engine
    if engine == "auto":
        engine = _choose_engine(pdf_path, log)

    if engine == "text":
        from .engine_text import extract as extract_text

        lines, statement = extract_text(str(pdf_path))
    elif engine == "ocr":
        from .engine_ocr import extract as extract_ocr

        lines, statement = extract_ocr(
            str(pdf_path),
            scale=args.ocr_scale,
            progress=None if args.quiet else (lambda d, t: log(f"  OCR page {d}/{t}")),
        )
    else:
        from .engine_vision import extract as extract_vision

        def progress(done, total):
            log(f"  transcribed batch {done}/{total}")

        lines, statement = extract_vision(
            str(pdf_path),
            pages_per_call=args.pages_per_call,
            concurrency=args.concurrency,
            effort=args.effort,
            progress=None if args.quiet else progress,
        )

    statement.transactions = dedupe(stitch(lines, wrap_join=args.wrap_join))
    return statement, validate(statement)


def _choose_engine(pdf_path: Path, log) -> str:
    from .engine_text import has_text_layer

    if has_text_layer(str(pdf_path)):
        log("  text layer found - using the text engine")
        return "text"
    log("  no text layer (scanned PDF) - using Claude vision")
    return "vision"


def _resolve_inputs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(path.glob("*.pdf")))
        else:
            out.append(path)
    return out


def _output_path(pdf_path: Path, args, many: bool) -> Path:
    if args.output:
        target = Path(args.output)
        if many or target.is_dir() or target.suffix.lower() != ".xlsx":
            target.mkdir(parents=True, exist_ok=True)
            return target / f"{pdf_path.stem}.xlsx"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    return pdf_path.with_suffix(".xlsx")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    pdfs = _resolve_inputs(args.pdfs)
    if not pdfs:
        print("no PDF files found", file=sys.stderr)
        return 2

    failures = 0
    for pdf_path in pdfs:
        if not pdf_path.is_file():
            print(f"{pdf_path}: not found", file=sys.stderr)
            failures += 1
            continue

        log(f"{pdf_path.name}:")
        try:
            statement, report = convert(pdf_path, args, log)
        except Exception as exc:  # surfaced per file so a batch keeps going
            print(f"{pdf_path.name}: {exc}", file=sys.stderr)
            failures += 1
            continue

        out_path = _output_path(pdf_path, args, many=len(pdfs) > 1)
        write_excel(statement, str(out_path), report)

        if args.json_out:
            json_path = (
                Path(args.json_out)
                if len(pdfs) == 1 and Path(args.json_out).suffix == ".json"
                else Path(args.json_out) / f"{pdf_path.stem}.json"
            )
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(statement.to_dict(), indent=2), encoding="utf-8")

        log(_indent(summarise(report)))
        print(f"{out_path}  ({len(statement.transactions)} transactions)")

        if report.errors:
            print(
                f"{pdf_path.name}: {len(report.errors)} validation error(s) - see the "
                "Validation sheet before using this file",
                file=sys.stderr,
            )
            if args.strict:
                failures += 1

    return 1 if failures else 0


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
