from flask import escape


def greeting(name: str) -> str:
    return str(escape(name))
