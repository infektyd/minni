import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_split_statements_ignores_semicolons_and_keywords_inside_quotes():
    from minni.migrations import _split_statements

    sql = """
    INSERT INTO demo(value) VALUES ('hello; BEGIN not a block; END');
    CREATE TRIGGER demo_ai AFTER INSERT ON demo BEGIN
      INSERT INTO audit(value) VALUES ("trigger; END literal");
    END;
    /* BEGIN comment; END comment; */
    INSERT INTO demo(value) VALUES ('done');
    """

    statements = list(_split_statements(sql))

    assert len(statements) == 3
    assert statements[0].startswith("INSERT INTO demo")
    assert statements[1].startswith("CREATE TRIGGER")
    assert statements[1].endswith("END")
    assert statements[2].endswith("('done')")
