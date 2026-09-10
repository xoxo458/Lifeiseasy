"""Extraction engine for scanned statements, using Claude's vision on the PDF.

The PDF is split into small page groups; each group is sent to Claude as a
document block and comes back as structured JSON. Wrapped cells are returned as
*lists of printed line fragments* so that rejoining stays deterministic in our
code rather than depending on the model's formatting choices.
"""

from __future__ import annotations

import base64
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pypdfium2 as pdfium

from .models import Statement, parse_amount, parse_date
from .parse import RawLine

MODEL = os.environ.get("MCBX_MODEL", "claude-opus-5")
DEFAULT_PAGES_PER_CALL = 4

_TEXT_LINE_FIELDS = {
    "description_lines": "description",
    "remitter_name_lines": "remitter_name",
    "remitter_iban_lines": "remitter_iban",
    "remitter_bank_lines": "remitter_bank",
}

_SYSTEM = """You transcribe MCB Bank Limited account statements into JSON.

You are a transcriber, not an analyst. Copy exactly what is printed:
- Never compute, correct, infer or round a figure. If a cell is blank, return "".
- Keep every digit, separator and decimal exactly as printed ("1,144,333.79").
- Keep leading zeros in branch codes and cheque/reference numbers ("0069", "0000014203").
- Debit and credit are separate columns. A figure goes in exactly one of them -
  use the column it is printed under, never the sign of the balance movement.
- Ignore the diagonal "For Internal Use Only" watermark, the CamScanner mark,
  the repeated page header/footer, and the column header row.

A statement row often wraps over several printed lines. For each wrapped column,
return the printed line fragments in order, as separate array entries. Do not
join them and do not add spaces or hyphens - the caller rejoins them.

If the first row on the first page in this batch has no transaction date because
it is the tail of a row that began on an earlier page, set
"continues_from_previous" to true on that row and leave its date fields "".

Return every transaction row on these pages, in printed order. Do not summarise,
truncate, or skip repeated-looking rows - near-identical consecutive rows are
common and each one must appear."""

_TXN_SCHEMA = {
    "type": "object",
    "properties": {
        "continues_from_previous": {
            "type": "boolean",
            "description": "True only for a leading fragment of a row started on an earlier page.",
        },
        "tran_date": {"type": "string", "description": "As printed, e.g. 01-AUG-26. '' if absent."},
        "effect_date": {"type": "string"},
        "branch": {"type": "string", "description": "Tran. Br. as printed, leading zeros kept."},
        "description_lines": {"type": "array", "items": {"type": "string"}},
        "remitter_name_lines": {"type": "array", "items": {"type": "string"}},
        "remitter_iban_lines": {"type": "array", "items": {"type": "string"}},
        "remitter_bank_lines": {"type": "array", "items": {"type": "string"}},
        "ref_no": {"type": "string"},
        "debit": {"type": "string"},
        "credit": {"type": "string"},
        "balance": {"type": "string"},
    },
    "required": [
        "continues_from_previous", "tran_date", "effect_date", "branch",
        "description_lines", "remitter_name_lines", "remitter_iban_lines",
        "remitter_bank_lines", "ref_no", "debit", "credit", "balance",
    ],
    "additionalProperties": False,
}

_META_SCHEMA = {
    "type": "object",
    "properties": {
        "account_title": {"type": "string"},
        "address": {"type": "string"},
        "account_no": {"type": "string"},
        "iban": {"type": "string"},
        "account_type": {"type": "string"},
        "currency": {"type": "string"},
        "branch": {"type": "string"},
        "opened_on": {"type": "string"},
        "period_from": {"type": "string"},
        "period_to": {"type": "string"},
        "statement_datetime": {"type": "string"},
        "opening_balance": {"type": "string"},
    },
    "required": [
        "account_title", "address", "account_no", "iban", "account_type", "currency",
        "branch", "opened_on", "period_from", "period_to", "statement_datetime",
        "opening_balance",
    ],
    "additionalProperties": False,
}

_TOTALS_SCHEMA = {
    "type": "object",
    "properties": {
        "total_dr_count": {"type": "string"},
        "total_cr_count": {"type": "string"},
        "sum_dr": {"type": "string"},
        "sum_cr": {"type": "string"},
        "available_balance": {"type": "string"},
        "closing_balance": {"type": "string"},
    },
    "required": [
        "total_dr_count", "total_cr_count", "sum_dr", "sum_cr",
        "available_balance", "closing_balance",
    ],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {"type": "array", "items": _TXN_SCHEMA},
        "meta": _META_SCHEMA,
        "totals": _TOTALS_SCHEMA,
    },
    "required": ["transactions", "meta", "totals"],
    "additionalProperties": False,
}


class VisionEngineError(RuntimeError):
    pass


def page_count(pdf_path: str) -> int:
    doc = pdfium.PdfDocument(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def _chunk_bytes(pdf_path: str, page_indices: list[int]) -> bytes:
    """Build a small PDF containing just the given pages."""
    src = pdfium.PdfDocument(pdf_path)
    dst = pdfium.PdfDocument.new()
    try:
        dst.import_pages(src, pages=page_indices)
        buf = io.BytesIO()
        dst.save(buf)
        return buf.getvalue()
    finally:
        dst.close()
        src.close()


def extract(
    pdf_path: str,
    pages_per_call: int = DEFAULT_PAGES_PER_CALL,
    concurrency: int = 3,
    effort: str = "high",
    progress=None,
) -> tuple[list[RawLine], Statement]:
    """Transcribe the whole statement, returning visual lines plus meta/totals."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise VisionEngineError(
            "the vision engine needs the anthropic SDK: pip install anthropic"
        ) from exc

    total_pages = page_count(pdf_path)
    chunks = [
        list(range(start, min(start + pages_per_call, total_pages)))
        for start in range(0, total_pages, pages_per_call)
    ]
    client = anthropic.Anthropic()

    def run(index_and_pages):
        index, pages = index_and_pages
        payload = _call_claude(client, _chunk_bytes(pdf_path, pages), pages, total_pages, effort)
        if progress:
            progress(index + 1, len(chunks))
        return payload

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        payloads = list(pool.map(run, enumerate(chunks)))

    statement = Statement(source_file=pdf_path, engine="vision")
    lines: list[RawLine] = []
    for pages, payload in zip(chunks, payloads):
        first_page = pages[0] + 1
        lines.extend(_payload_lines(payload, first_page))
        _merge_meta(statement, payload.get("meta") or {})
        _merge_totals(statement, payload.get("totals") or {})

    if not lines:
        raise VisionEngineError(f"Claude returned no transaction rows for {pdf_path}")
    return lines, statement


def _call_claude(client, pdf_bytes: bytes, pages: list[int], total_pages: int, effort: str) -> dict:
    label = (
        f"pages {pages[0] + 1}-{pages[-1] + 1} of {total_pages}"
        if len(pages) > 1
        else f"page {pages[0] + 1} of {total_pages}"
    )
    prompt = (
        f"This document contains {label} of an MCB account statement.\n\n"
        "Transcribe every transaction row printed on these pages.\n"
        "Fill 'meta' only from the account header block if it is visible here, and "
        "'totals' only from the summary block after the last transaction if it is "
        "visible here; otherwise return empty strings in those objects."
    )

    try:
        message = _stream(client, pdf_bytes, prompt, effort)
    except VisionEngineError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise VisionEngineError(
                "no Anthropic credentials found - set ANTHROPIC_API_KEY to use the "
                "vision engine on scanned PDFs"
            ) from exc
        raise VisionEngineError(f"transcription of {label} failed: {exc}") from exc

    if message.stop_reason == "refusal":
        detail = getattr(message.stop_details, "explanation", "") or ""
        raise VisionEngineError(f"Claude declined to transcribe {label}: {detail}".strip())
    if message.stop_reason == "max_tokens":
        raise VisionEngineError(
            f"transcription of {label} hit the output limit - rerun with a smaller "
            "--pages-per-call"
        )

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionEngineError(f"could not parse Claude's response for {label}: {exc}") from exc


def _stream(client, pdf_bytes: bytes, prompt: str, effort: str):
    """One structured-output request carrying the page group as a PDF document."""
    with client.messages.stream(
        model=MODEL,
        max_tokens=64000,
        system=_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
        },
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    ) as stream:
        return stream.get_final_message()


def _is_auth_error(exc: Exception) -> bool:
    """True for a missing/invalid API key, whichever layer raised it."""
    if type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError"}:
        return True
    text = str(exc).lower()
    return "could not resolve authentication" in text or "x-api-key" in text


def _payload_lines(payload: dict, first_page: int) -> list[RawLine]:
    """Expand each returned transaction back into one RawLine per printed line."""
    lines: list[RawLine] = []
    for txn in payload.get("transactions") or []:
        fragments = {
            field: [s for s in (txn.get(key) or []) if str(s).strip()]
            for key, field in _TEXT_LINE_FIELDS.items()
        }
        depth = max([len(v) for v in fragments.values()] + [1])

        for i in range(depth):
            cells: dict[str, str] = {}
            for field, values in fragments.items():
                if i < len(values):
                    cells[field] = str(values[i]).strip()
            if i == 0 and not txn.get("continues_from_previous"):
                cells["tran_date"] = str(txn.get("tran_date") or "").strip()
                cells["effect_date"] = str(txn.get("effect_date") or "").strip()
                cells["branch"] = str(txn.get("branch") or "").strip()
                cells["ref_no"] = str(txn.get("ref_no") or "").strip()
                cells["debit"] = str(txn.get("debit") or "").strip()
                cells["credit"] = str(txn.get("credit") or "").strip()
                cells["balance"] = str(txn.get("balance") or "").strip()
            elif i == 0:
                # Tail of a row from an earlier page: carry only the trailing cells.
                for key in ("ref_no", "debit", "credit", "balance"):
                    value = str(txn.get(key) or "").strip()
                    if value:
                        cells[key] = value
            if cells:
                lines.append(RawLine(cells=cells, page=first_page))
    return lines


def _merge_meta(statement: Statement, raw: dict) -> None:
    meta = statement.meta
    for field in ("account_title", "address", "account_no", "iban", "account_type",
                  "currency", "branch", "statement_datetime"):
        value = str(raw.get(field) or "").strip()
        if value and not getattr(meta, field):
            setattr(meta, field, value)
    for field in ("opened_on", "period_from", "period_to"):
        value = parse_date(str(raw.get(field) or ""))
        if value and not getattr(meta, field):
            setattr(meta, field, value)
    if meta.opening_balance is None:
        meta.opening_balance = parse_amount(str(raw.get("opening_balance") or ""))


def _merge_totals(statement: Statement, raw: dict) -> None:
    totals = statement.totals
    for field in ("total_dr_count", "total_cr_count"):
        if getattr(totals, field) is None:
            value = str(raw.get(field) or "").replace(",", "").strip()
            if value.isdigit():
                setattr(totals, field, int(value))
    for field in ("sum_dr", "sum_cr", "available_balance", "closing_balance"):
        if getattr(totals, field) is None:
            value = parse_amount(str(raw.get(field) or ""))
            if value is not None:
                setattr(totals, field, value)
