"""Minimal INI editing that leaves comments and layout alone outside the changed lines.

``configparser`` drops every comment when it writes a file, which would wreck a
hand-edited ``.rgadget``; this edits only the lines that must change.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY = re.compile(r"^([^\s=:#;\[][^=:]*?)\s*[=:]\s*(.*?)\s*$")
_INLINE_COMMENT = re.compile(r"\s+[;#].*$")


def _is_continuation(line: str) -> bool:
    stripped = line.strip()
    return line[:1] in " \t" and bool(stripped) and stripped[0] not in "#;"


def _line(key: str, value: str, comment: str = "") -> str:
    # Continuation lines of a multi-line value must be indented.
    return f"{key} = {value.replace(chr(10), chr(10) + '  ')}{comment}\n"


def set_section(text: str, name: str, values: Mapping[str, str]) -> str:
    """Return ``text`` with section ``[name]`` holding exactly ``values``.

    Unchanged keys, comments and other sections are kept byte for byte.  Changed keys
    are rewritten in place (keeping any inline comment), keys not in ``values`` are
    deleted, and new keys are added after the section's last line.  A missing section
    is appended to the end of the file.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    headers = {i: m[1].strip() for i, line in enumerate(lines) if (m := _SECTION.match(line))}
    start = next((i for i, section in headers.items() if section == name), None)
    pending = {k.lower(): v for k, v in values.items()}
    if start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines += [f"[{name}]\n", *(_line(k, v) for k, v in pending.items())]
        return "".join(lines)

    end = next((i for i in headers if i > start), len(lines))
    out = lines[: start + 1]
    insert_at = len(out)  # just after the section's last non-blank line
    i = start + 1
    while i < end:
        line = lines[i]
        i += 1
        m = _KEY.match(line)
        if not m:  # blank line, comment or stray text: keep it
            out.append(line)
            if line.strip():
                insert_at = len(out)
            continue
        continuation = []
        while i < end and _is_continuation(lines[i]):
            continuation.append(lines[i])
            i += 1
        key = m[1].strip().lower()
        if key not in pending:
            continue  # deleted, along with any continuation lines
        new = pending.pop(key)
        raw = m[2]
        comment = _INLINE_COMMENT.search(raw)
        old = "\n".join([_INLINE_COMMENT.sub("", raw).strip()] + [c.strip() for c in continuation])
        if old == new:
            out += [line, *continuation]
        else:
            out.append(_line(m[1].strip(), new, comment[0] if comment else ""))
        insert_at = len(out)
    out[insert_at:insert_at] = [_line(k, v) for k, v in pending.items()]
    return "".join(out + lines[end:])
