from models import User, serialize


def test_serialization() -> None:
    assert serialize(User(name="Ada")) == {"name": "Ada"}
