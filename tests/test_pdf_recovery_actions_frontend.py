"""Recovery actions must remain usable while the editable draft is locked."""
from html.parser import HTMLParser
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


class RecoveryControls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("@click") == "retryFailedImportDraft(draft)":
            self.controls.append((attrs, list(self.stack)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def test_query_action_is_not_disabled_by_locked_draft_ancestor():
    parser = RecoveryControls()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    assert len(parser.controls) == 1
    attrs, ancestors = parser.controls[0]
    assert not any(tag == "fieldset" and (":disabled" in attrs or "disabled" in attrs)
                   for tag, attrs in ancestors), "Locked draft fieldset disables its recovery button"
    status = next(attrs for _, attrs in reversed(ancestors) if "draft._save_status" in attrs.get("v-if", ""))
    assert "draft._save_message" in status["v-if"], "not_found guidance must stay visible when status returns to idle"
    assert "draft._save_status !== 'unknown'" in attrs[":disabled"], "Result lookup must not require an editable/confirmed draft"
