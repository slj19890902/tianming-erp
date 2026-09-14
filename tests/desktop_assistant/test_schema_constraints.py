import unittest
from desktop_assistant.migration import canonical_table_sql


class SchemaConstraintTests(unittest.TestCase):
    def test_constraint_order_only_is_equivalent(self):
        a='CREATE TABLE t (id INTEGER, n INTEGER, PRIMARY KEY(id), CHECK(n > 0), FOREIGN KEY(n) REFERENCES p(id))'
        b='CREATE TABLE t (id INTEGER, n INTEGER, FOREIGN KEY(n) REFERENCES p(id), PRIMARY KEY(id), CHECK(n > 0))'
        self.assertEqual(canonical_table_sql(a),canonical_table_sql(b))

    def test_changed_or_missing_constraints_and_column_order_are_rejected(self):
        source='CREATE TABLE t (id INTEGER, n INTEGER, CHECK(n > 0), FOREIGN KEY(n) REFERENCES p(id))'
        for changed in (source.replace('n > 0','n >= 0'),source.replace(', FOREIGN KEY(n) REFERENCES p(id)',''),
                        source.replace('id INTEGER, n INTEGER','n INTEGER, id INTEGER'),
                        source.replace('REFERENCES p(id)','REFERENCES q(id)'),source+' WITHOUT ROWID'):
            self.assertNotEqual(canonical_table_sql(source),canonical_table_sql(changed))

    def test_quoted_commas_parentheses_escapes_and_constraint_names_are_preserved(self):
        a="CREATE TABLE t (s TEXT DEFAULT 'a,b)', CONSTRAINT a CHECK(s <> 'x''y('), UNIQUE(s))"
        b="CREATE TABLE t (s TEXT DEFAULT 'a,b)', UNIQUE(s), CONSTRAINT a CHECK(s <> 'x''y('))"
        self.assertEqual(canonical_table_sql(a),canonical_table_sql(b))
        self.assertNotEqual(canonical_table_sql(a),canonical_table_sql(b.replace('CONSTRAINT a','CONSTRAINT b')))

    def test_unparseable_table_fails_closed(self):
        with self.assertRaises(ValueError):
            canonical_table_sql('CREATE TABLE t (id INTEGER')
