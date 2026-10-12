"""Find-and-replace that tolerates the small mistakes models make.

Order of attempts: exact match; whitespace-insensitive match; then a helpful
error that quotes the closest real lines so the next attempt can succeed.
"""

from __future__ import annotations

import difflib
import re


def apply_edit(text: str, old: str, new: str) -> tuple[str | None, str]:
    if not old:
        return None, "old_text is empty."
    count = text.count(old)
    if count == 1:
        return text.replace(old, new, 1), "Edited."
    if count > 1:
        return None, f"old_text appears {count} times; include more surrounding text so it is unique."

    # Whitespace-insensitive: any run of whitespace in old_text matches any run in the file.
    pattern = r"\s+".join(re.escape(part) for part in old.strip().split())
    if pattern:
        matches = list(re.finditer(pattern, text))
        if len(matches) == 1:
            m = matches[0]
            return text[: m.start()] + new.strip("\n") + text[m.end():], "Edited (matched ignoring whitespace)."
        if len(matches) > 1:
            return None, f"old_text matches {len(matches)} places; include more surrounding text so it is unique."

    lines = text.splitlines()
    probe = old.strip().splitlines()[0].strip() if old.strip() else ""
    close = difflib.get_close_matches(probe, [ln.strip() for ln in lines], n=3, cutoff=0.45)
    if close:
        hint = "\n".join(f"  {c}" for c in close)
        return None, f"old_text was not found. Closest lines in the file:\n{hint}\nCopy the text exactly from one of these."
    return None, "old_text was not found. Read the file again and copy the exact text you want to change."
