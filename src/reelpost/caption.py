"""Caption text and hashtags for Instagram and Facebook."""

from __future__ import annotations

import re

from reelpost.constants import CAPTION_LIMIT, MAX_HASHTAGS
from reelpost.errors import ReelpostError

_HASHTAG = re.compile(r"#([\w]+)", flags=re.UNICODE)
_TAG = re.compile(r"[\w]+", flags=re.UNICODE)
_LETTER = re.compile(r"[^\W\d_]", flags=re.UNICODE)


def parse_tags(values: list[str]) -> list[str]:
    """Split comma- or space-separated tags, strip a leading #, and drop duplicates."""
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        for piece in re.split(r"[\s,]+", value.strip()):
            if not piece:
                continue
            tag = piece[1:] if piece.startswith("#") else piece
            if not tag or not _TAG.fullmatch(tag) or not _LETTER.search(tag):
                raise ReelpostError(
                    f"Invalid tag {piece!r}. Use letters, numbers, and underscores, "
                    "and include at least one letter."
                )
            if len(tag) > 100:
                raise ReelpostError(f"Tag #{tag} is longer than 100 characters.")
            key = tag.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(tag)
    if len(tags) > MAX_HASHTAGS:
        raise ReelpostError(f"Use at most {MAX_HASHTAGS} hashtags.")
    return tags


def compose_caption(description: str, tags: list[str], *, limit: int = CAPTION_LIMIT) -> str:
    """Join the description and any hashtags that are not already in it."""
    description = description.replace("\r\n", "\n").strip()
    present = {match.group(1).casefold() for match in _HASHTAG.finditer(description)}
    extra = [tag for tag in tags if tag.casefold() not in present]
    hashtag_count = len(present) + len(extra)
    if hashtag_count > MAX_HASHTAGS:
        raise ReelpostError(
            f"The description plus tags contain {hashtag_count} hashtags. "
            f"The limit is {MAX_HASHTAGS}."
        )
    line = " ".join(f"#{tag}" for tag in extra)
    if description and line:
        caption = f"{description}\n\n{line}"
    else:
        caption = description or line
    if len(caption) > limit:
        raise ReelpostError(
            f"The description is {len(caption)} characters. The limit is {limit}."
        )
    return caption
