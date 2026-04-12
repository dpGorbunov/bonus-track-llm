"""Input sanitization utilities for PostgreSQL-safe text storage."""


def sanitize_text(text: str | None) -> str | None:
    """Remove null bytes and other chars that PostgreSQL can't store.

    PostgreSQL TEXT/VARCHAR columns reject the null byte (\\x00).
    This helper strips it and trims surrounding whitespace so any
    user-supplied string can be safely persisted.
    """
    if text is None:
        return None
    return text.replace("\x00", "").strip()
