from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import StringIO
from typing import NamedTuple

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, escape, newline
from markdown_it.token import Token

_OTHER_LINK = re.compile(r"\]\s*(?:\(|\[|:)|<a\s", re.IGNORECASE)
_INDEX_ENTRY = re.compile(
    r"\s*(?:[-*+]\s+)?\[[^\]\n]+\]\((?:\./)?(?P<topic>[^/\\():#?\n]+\.md)\)(?:\s+[\u2014\u2013-]\s+[^\[\]\n]+)?\s*"
)
_INDEX_TITLE = re.compile(
    r"#{1,6}\s+(?:(?:auto|project|subagent|agent)\s+)?memory(?:\s+index)?\s*#*",
    re.IGNORECASE,
)
MAX_INDEX_LINES = 65_536


class AmbiguousIndex(ValueError):
    pass


class OrderedPosition(NamedTuple):
    opener: int
    ordinal: int


class ListItem(NamedTuple):
    opener: int
    ordered: OrderedPosition | None


@dataclass(frozen=True)
class IndexLine:
    source: str
    topic: str | None
    block: tuple[str, ...]
    list_items: tuple[ListItem, ...]
    inline: tuple[tuple[object, ...], ...] = ()

    @property
    def literal(self) -> bool:
        return bool(self.block and self.block[-1] in {"fence", "code_block"})


def _locate_breaks(
    rule: Callable[[StateInline, bool], bool],
) -> Callable[[StateInline, bool], bool]:
    # Block maps omit the physical newlines consumed inside a multiline inline token.
    def located(state: StateInline, silent: bool) -> bool:
        matched = rule(state, silent)
        if (
            matched
            and not silent
            and state.tokens
            and state.tokens[-1].type in {"softbreak", "hardbreak"}
        ):
            state.tokens[-1].meta["next_offset"] = state.pos
        return matched

    return located


def _inline_groups(token: Token) -> list[tuple[int, int, list[Token]]]:
    assert token.map
    start, end = token.map
    breaks = [i for i, char in enumerate(token.content) if char == "\n"]
    groups = []
    first = start
    children: list[Token] = []
    for child in token.children or ():
        offset = child.meta.get("next_offset")
        if offset is None:
            children.append(child)
        else:
            stop = start + bisect_left(breaks, offset)
            groups.append((first, stop, children))
            first = stop
            children = []
    groups.append((first, end, children))
    return groups


def _list_items(
    tokens: list[Token], source: list[str], numbers: Sequence[int]
) -> list[tuple[ListItem, ...]]:
    ownership: list[tuple[ListItem, ...]] = [()] * len(source)
    lists: list[OrderedPosition | None] = []
    for token in tokens:
        if token.type == "ordered_list_open":
            assert token.map
            lists.append(OrderedPosition(numbers[token.map[0]], int(token.attrs.get("start", 1))))
        elif token.type == "bullet_list_open":
            lists.append(None)
        elif token.type in {"ordered_list_close", "bullet_list_close"}:
            lists.pop()
        elif token.type == "list_item_open":
            assert token.map
            start, end = token.map
            ordered = lists[-1]
            owners = (*ownership[start], ListItem(numbers[start], ordered))
            if ordered is not None:
                lists[-1] = OrderedPosition(ordered.opener, ordered.ordinal + 1)
            for i in range(start, end):
                if source[i].strip(" \t\r\n"):
                    ownership[i] = owners
    return ownership


def index_lines(index: str, *, original_lines: Sequence[int] | None = None) -> list[IndexLine]:
    breaks = index.count("\r") + index.count("\n") - index.count("\r\n")
    if breaks + bool(index and not index.endswith(("\r", "\n"))) > MAX_INDEX_LINES:
        raise ValueError(
            f"Reduce the Claude memory index to {MAX_INDEX_LINES:,} physical lines before review"
        )
    source = list(StringIO(index, newline=""))
    parser = MarkdownIt("commonmark")
    parser.inline.ruler.at("newline", _locate_breaks(newline))
    parser.inline.ruler.at("escape", _locate_breaks(escape))
    stack: list[str] = []
    tokens = parser.parse(index.removeprefix("\ufeff"))
    owners = _list_items(
        tokens, source, range(len(source)) if original_lines is None else original_lines
    )
    lines = [IndexLine(line, None, (), owners[i]) for i, line in enumerate(source)]
    for token in tokens:
        if token.nesting < 0:
            stack.pop()
            continue
        if token.nesting > 0:
            stack.append(token.type)
        if token.map is None or token.nesting:
            continue
        block = (*stack, token.type)
        if token.type == "fence" and stack:
            raise AmbiguousIndex(
                "Index links in an ambiguous Markdown context cannot be separated exactly"
            )
        if token.type != "inline":
            for i in range(*token.map):
                lines[i] = IndexLine(source[i], None, block, owners[i])
            continue
        for first, stop, children in _inline_groups(token):
            semantics = tuple(
                (c.type, c.markup, c.content, tuple(c.attrs.items())) for c in children
            )
            for i in range(first, stop):
                match = _INDEX_ENTRY.fullmatch(
                    source[i].removeprefix("\ufeff") if i == 0 else source[i]
                )
                topic = None
                if (
                    stop == first + 1
                    and match
                    and stack[-1:] == ["paragraph_open"]
                    and children
                    and children[0].type == "link_open"
                    and children[0].attrGet("href")
                    in {
                        parser.normalizeLink(match["topic"]),
                        parser.normalizeLink("./" + match["topic"]),
                    }
                ):
                    topic = match["topic"]
                lines[i] = IndexLine(source[i], topic, block, owners[i], semantics)
    return lines


def inline_content(index: str) -> tuple[str, str | None]:
    try:
        lines = [line.source for line in index_lines(index) if line.topic is None]
    except AmbiguousIndex as exc:
        return index.strip(), str(exc)
    retained = [line.strip() for line in lines if line.strip()]
    if not retained or (len(retained) == 1 and _INDEX_TITLE.fullmatch(retained[0])):
        return "", None
    if all(line.startswith("#") for line in retained):
        return index.strip(), "Inline headings and index structure cannot be separated exactly"
    ambiguous = any(
        _OTHER_LINK.search(line) or line.startswith(("```", "~~~")) for line in retained
    )
    if ambiguous:
        return index.strip(), "Inline prose and index links cannot be separated exactly"
    return "".join(lines).strip(), None
