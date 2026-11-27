"""Защита POST от кросс-сайтовых запросов: нужен свой заголовок и, если есть Origin, он должен совпадать с Host."""

from collections.abc import Mapping
from urllib.parse import urlsplit

HEADER = "X-Lab-Request"


def csrf_error(headers: Mapping[str, str]) -> str | None:
    """Текст ошибки или None, если запрос допустим. Страница с чужого сайта не может поставить свой заголовок без preflight."""
    if headers.get(HEADER) != "1":
        return f"нужен заголовок {HEADER}: 1"
    origin = headers.get("Origin")
    if origin is not None and urlsplit(origin).netloc != headers.get("Host"):
        return "чужой Origin"
    return None
