import imp


def module_suffixes() -> list[tuple[str, str, int]]:
    return imp.get_suffixes()
