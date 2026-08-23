import hashlib


def normalize_label_name(name: str) -> str:
    return "".join(name.split()).lower()


def stable_text_hash(text: str, length: int = 16) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest[:length]


def note_dedupe_key(book_id: str, note_id: str, position: str, text: str) -> str:
    if note_id:
        return f"note:{note_id}"
    return f"note_fallback:{book_id}:{position}:{stable_text_hash(text)}"


def daily_record_key(book_id: str, record_date: str) -> str:
    return f"daily:{book_id}:{record_date}"


def daily_stats_key(record_date: str) -> str:
    return f"daily-stats:{record_date}"
