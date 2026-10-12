"""Structure-aware chunks with bounded context, source offsets and no redundant tail.

Token budget uses UTF-8 byte count as a conservative upper bound for byte-based
tokenizers. It is deliberately not a claim about the provider's exact token count.
Offsets refer to the extracted canonical text (units joined by newline), not PDF
coordinates or original binary-file offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from app.config import get_settings
from app.rag.document_extract import TextUnit

CHUNKING_VERSION = "2"


def token_bound(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass
class Chunk:
    text: str
    pages: tuple[int, ...] = ()
    from_fields: bool = False
    body: str = ""
    section: tuple[str, ...] = ()
    kind: str = "paragraph"
    start: int = 0
    end: int = 0
    continued: bool = False
    frame_header: str = ""
    frame_footer: str = ""


def _prefix(source: str, section: tuple[str, ...]) -> str:
    labels = ([f"来源: {source}"] if source else []) + (["章节: " + " > ".join(section)] if section else [])
    return "\n".join(labels) + ("\n\n" if labels else "")


def _end(text: str, start: int, chars: int, tokens: int) -> int:
    lo, hi = start, min(len(text), start + chars)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if token_bound(text[start:mid]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _plain_units(units: list[TextUnit]):
    """Split explicit chapter/Markdown-style heading lines even inside a PDF page.

    No font/layout or semantic inference; ordered list items aren't headings.
    """
    heading = re.compile(r"(?m)^(?:第[一二三四五六七八九十百\d]+[章节][^\n]{0,80}|#{1,6}[ \t]+[^\n]{1,80})$")
    for unit in units:
        if unit.section or unit.kind != "paragraph":
            yield unit
            continue
        body = unit.text.strip()
        starts = sorted({0, *(m.start() for m in heading.finditer(body)), len(body)})
        for start, end in zip(starts, starts[1:]):
            if body[start:end].strip():
                yield replace(unit, text=body[start:end].strip())


def pack_units(units: list[TextUnit], size: int | None = None,
               overlap: int | None = None, *, source: str = "") -> list[Chunk]:
    s = get_settings()
    size = s.rag_chunk_chars if size is None else size
    overlap = s.rag_chunk_overlap if overlap is None else overlap
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("切块长度必须为正，重叠必须非负且小于切块长度")
    out: list[Chunk] = []
    offset = 0
    inferred: tuple[str, ...] = ()
    for unit in (_plain_units(units) if s.rag_detect_plain_headings else units):
        body = (unit.text or "").strip()
        if not body:
            continue
        # PDF/TXT 只识别明确的行式标题；不猜测字体、视觉版面或主题。
        section = unit.section
        if not section and s.rag_detect_plain_headings:
            first = body.splitlines()[0]
            if re.fullmatch(r"(?:第[一二三四五六七八九十百\d]+[章节]|#{1,6}\s+).{0,80}", first):
                title = first.lstrip("# ")
                if "节" in first and inferred and re.match(r"第.+章", inferred[0]):
                    inferred = (inferred[0], title)
                else:
                    inferred = (title,)
            section = inferred
        kind = unit.kind
        limit = size
        if size == s.rag_chunk_chars:
            limit = {"table": s.rag_table_chunk_chars, "code": s.rag_code_chunk_chars}.get(kind, size)
        prefix = _prefix(source, section)
        # A huge heading must not consume the entire body budget.
        max_prefix_chars = min(limit // 3, 300)
        prefix = prefix[:max_prefix_chars]
        while prefix and token_bound(prefix) > s.rag_chunk_tokens // 3:
            prefix = prefix[:-1]
        if prefix and not prefix.endswith("\n\n"):
            prefix = prefix.rstrip() + "\n\n"
        chars = limit - len(prefix)
        tokens = s.rag_chunk_tokens - token_bound(prefix)
        frame_open, frame_close = "", ""
        if kind == "code":
            first_line = body.splitlines()[0]
            fence = re.match(r"^(`{3,}|~{3,})(.*)$", first_line)
            if fence:
                frame_open, frame_close = first_line + "\n", "\n" + fence.group(1)
        elif kind == "table" and len(body.splitlines()) >= 2:
            lines = body.splitlines()
            if re.fullmatch(r"[| :\-]+", lines[1]) and "|" in lines[0]:
                frame_open = "\n".join(lines[:2]) + "\n"
        # Very wide tables must not consume the entire chunk with repeated headers.
        if len(frame_open) > chars // 3 or token_bound(frame_open) > tokens // 3:
            if kind == "table":
                stop = _end(frame_open, 0, max(1, chars // 3 - 12), max(1, tokens // 3 - 32))
                frame_open = "[表头节选]\n" + frame_open[:stop] + "\n"
            elif kind == "code":
                # Preserve actual source in body; use a short valid fence for framing.
                frame_open, frame_close = "```\n", "\n```"
        # Reserve framing space even for continuation fragments.
        chars -= len(frame_open) + len(frame_close)
        tokens -= token_bound(frame_open + frame_close)
        start = 0
        while start < len(body):
            end = _end(body, start, chars, tokens)
            if end <= start:
                raise ValueError("切块预算不足以容纳一个字符，请增大预算")
            if end < len(body):
                # Prefer row/line boundaries for tables/code; sentences for prose.
                pattern = r"\n" if kind in ("table", "code", "list") else r"[。！？!?；;\n]|\.(?=\s)"
                boundaries = [m.end() for m in re.finditer(pattern, body[start:end])]
                candidates = [start + n for n in boundaries if n >= (end - start) * s.rag_min_boundary_ratio]
                if candidates:
                    end = candidates[-1]
            piece = body[start:end]
            frame_header = frame_open if start > 0 else ""
            frame_footer = frame_close if end < len(body) else ""
            new = Chunk(prefix + frame_header + piece + frame_footer,
                        (unit.page,) if unit.page else (), unit.from_fields,
                        piece, section, kind, offset + start, offset + end, start > 0,
                        frame_header, frame_footer)
            # Only pack whole units with matching structure. Never append to a split tail.
            previous = out[-1] if out else None
            if start == 0 and end == len(body) and previous and not previous.continued and (
                previous.section == section and previous.kind == kind
                and kind not in ("code", "list") and not frame_open
                and previous.end + 1 == offset
                and len(prefix + previous.body + "\n" + piece) <= limit
                and token_bound(prefix + previous.body + "\n" + piece) <= s.rag_chunk_tokens
            ):
                previous.body += "\n" + piece
                previous.text = prefix + previous.body
                previous.end = offset + end
                previous.pages = tuple(sorted(set(previous.pages) | ({unit.page} if unit.page else set())))
                previous.from_fields |= unit.from_fields
            else:
                out.append(new)
            if end == len(body):
                break  # Do not emit a tail already covered by this window.
            # Structured rows/code lines don't need a second, partial copy.
            repeat = 0 if kind in ("table", "code", "list") else min(overlap, (end - start) // 3)
            start = end - repeat
        offset += len(body) + 1
    return out


def strategy_signature() -> str:
    """Parameter changes alter the recorded strategy, not just the code version."""
    import hashlib
    import json
    s = get_settings()
    values = {k: getattr(s, k) for k in ("rag_chunk_chars", "rag_chunk_tokens", "rag_chunk_overlap",
              "rag_table_chunk_chars", "rag_code_chunk_chars", "rag_excel_header_rows", "rag_min_boundary_ratio", "rag_detect_plain_headings")}
    return CHUNKING_VERSION + "-" + hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]
