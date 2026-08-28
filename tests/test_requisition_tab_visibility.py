from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: "_Node | None" = None
    children: list["_Node"] = field(default_factory=list)


class _TemplateParser(HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = _Node(
            tag,
            {name: value or "" for name, value in attrs},
            self.stack[-1],
        )
        self.stack[-1].children.append(node)
        if tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return


def _walk(node: _Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _ancestors(node: _Node):
    current: _Node | None = node
    while current is not None:
        yield current
        current = current.parent


def _requisition_tab_guards(node: _Node) -> list[str]:
    guards: list[str] = []
    for ancestor in _ancestors(node):
        expression = ancestor.attrs.get("v-if", "")
        for match in re.finditer(
            r"requisitionTab\s*===\s*(['\"])(pending|waiting|submitted)\1",
            expression,
        ):
            guards.append(match.group(2))
    return guards


def test_each_requisition_table_has_only_its_own_tab_visibility_guard() -> None:
    parser = _TemplateParser()
    parser.feed(INDEX)

    tables = {
        "pending": "requisition-pending-table",
        "waiting": "requisition-hold-table",
        "submitted": "reported-item-table",
    }
    all_nodes = list(_walk(parser.root))

    for expected_tab, table_class in tables.items():
        matched_tables = [
            node
            for node in all_nodes
            if node.tag == "table"
            and table_class in node.attrs.get("class", "").split()
        ]
        assert len(matched_tables) == 1, (expected_tab, len(matched_tables))

        guards = _requisition_tab_guards(matched_tables[0])
        assert guards == [expected_tab], (
            f"{expected_tab} table is nested under contradictory tab guards: {guards}"
        )
        def is_visible(active_tab: str) -> bool:
            return all(guard == active_tab for guard in guards)

        assert is_visible(expected_tab)
        for other_tab in tables.keys() - {expected_tab}:
            assert not is_visible(other_tab)
