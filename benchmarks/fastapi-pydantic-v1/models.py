from pydantic import BaseModel


class User(BaseModel):
    name: str


def serialize(user: User) -> dict[str, str]:
    return user.dict()
