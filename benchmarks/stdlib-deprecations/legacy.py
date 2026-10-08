from collections import Mapping
import asyncio


def is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def legacy_loop():
    return asyncio.get_event_loop()
