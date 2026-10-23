"""A retained/partial physical-fact schema cannot run an older reader."""
import sqlite3
import pytest

from desktop_assistant.reader_contract import target_readers


@pytest.mark.parametrize("table", ["delivery_fulfillment_revisions", "delivery_fulfillment_segments"])
def test_any_fulfillment_table_requires_signed_reader_even_if_empty(tmp_path, table):
    path = tmp_path / "reader.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY)")
    for invalid in ({}, {"reader_capabilities":{"delivery_fulfillment_facts_v1":True}}):
        with pytest.raises(ValueError, match="reader"):
            target_readers(path, {}, invalid, None, None, factory=False)
    target_readers(path, {}, {"reader_capabilities":{"delivery_fulfillment_facts_v1":1}}, None, None, factory=False)


def test_source_signed_reader_cannot_disappear_after_program_switch(tmp_path):
    path = tmp_path / "reader.sqlite3"
    sqlite3.connect(path).close()
    manifest = {"reader_capabilities":{"delivery_fulfillment_facts_v1":1}}
    with pytest.raises(ValueError, match="reader"):
        target_readers(path, manifest, {}, None, None, factory=False)
    target_readers(path, manifest, manifest, None, None, factory=False)
