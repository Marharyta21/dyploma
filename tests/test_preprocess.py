from reviews.preprocess import clean


def test_lowercases():
    assert clean("This Movie Was GREAT") == "this movie was great"


def test_removes_html():
    assert clean("first line<br /><br />second line") == "first line second line"


def test_collapses_whitespace_and_keeps_apostrophes():
    assert clean("  I   don't   like it!!  ") == "i don't like it"
