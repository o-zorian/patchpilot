def clamp(value: int, lower: int, upper: int) -> int:
    if value < lower:
        return upper
    if value > upper:
        return lower
    return value


def normalize_email(value: str) -> str:
    return value.lower()


def unique_tags(tags: list[str]) -> list[str]:
    return sorted(set(tags))
