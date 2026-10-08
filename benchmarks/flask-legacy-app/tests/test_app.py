from app import greeting


def test_greeting_escapes_html() -> None:
    assert greeting("<Ada>") == "&lt;Ada&gt;"
