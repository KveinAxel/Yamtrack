"""NeoDB provider for general Chinese-first book search."""

import re

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

BASE_URL = "https://neodb.social"
USER_AGENT = "KveinAxel-Yamtrack/0.1 (https://github.com/KveinAxel/Yamtrack)"
SEARCH_PAGE_SIZE = 20  # NeoDB catalog search uses a fixed server-side page size.
SEARCH_CACHE_VERSION = "v1"
UUID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,36}$")


def _raise_schema_error(details):
    """Raise a provider error for an unexpected NeoDB response."""
    message = f"Invalid NeoDB response: {details}"
    error = ValueError(message)
    raise services.ProviderAPIError(
        Sources.NEODB.value,
        error,
        message,
    ) from error


def canonical_uuid(media_id):
    """Return a validated NeoDB item UUID."""
    if not isinstance(media_id, str) or not UUID_PATTERN.match(media_id.strip()):
        msg = f"Invalid NeoDB item ID: {media_id}"
        raise ValueError(msg)
    return media_id.strip()


def localized_title(item):
    """Prefer a non-empty Simplified Chinese title, then the display title."""
    if not isinstance(item, dict):
        _raise_schema_error("expected an item object")

    localized = item.get("localized_title")
    if isinstance(localized, list):
        for entry in localized:
            if not isinstance(entry, dict) or entry.get("lang") != "zh-cn":
                continue
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    for field in ("display_title", "title"):
        title = item.get(field)
        if isinstance(title, str) and title.strip():
            return title.strip()

    return _raise_schema_error("item must provide a non-empty title")


def _item_image(item):
    """Return the item cover, falling back to the shared placeholder."""
    cover = item.get("cover_image_url")
    if isinstance(cover, str) and cover.strip():
        return cover.strip()
    return settings.IMG_NONE


def _optional_string(item, key):
    """Return a trimmed optional string."""
    value = item.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(item, key):
    """Return the non-empty strings of an optional list field."""
    values = item.get(key)
    if not isinstance(values, list):
        return []
    return [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _rating(item):
    """Return correctly typed NeoDB score fields."""
    score = item.get("rating")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None

    score_count = item.get("rating_count")
    if (
        isinstance(score_count, bool)
        or not isinstance(score_count, int)
        or score_count < 0
    ):
        score_count = None
    return score, score_count


def _publish_date(item):
    """Return the publish date derived from the year and optional month."""
    year = positive_int(item.get("pub_year"))
    if year is None:
        return None
    month = positive_int(item.get("pub_month"))
    if month is not None and 1 <= month <= 12:  # noqa: PLR2004
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def positive_int(value):
    """Return a strictly positive integer, or None for invalid values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _validate_book_category(item):
    """Reject NeoDB items of any category other than book."""
    if item.get("category") != MediaTypes.BOOK.value:
        _raise_schema_error("category must be book")


def _validate_search_item(item):
    """Validate and normalize the required fields of one search result."""
    if not isinstance(item, dict):
        _raise_schema_error("expected an item object")

    _validate_book_category(item)

    uuid = item.get("uuid")
    if not isinstance(uuid, str) or not UUID_PATTERN.match(uuid.strip()):
        _raise_schema_error("uuid must be a valid item ID")

    return {
        "media_id": uuid.strip(),
        "source": Sources.NEODB.value,
        "media_type": MediaTypes.BOOK.value,
        "title": localized_title(item),
        "image": _item_image(item),
    }


def search(media_type, query, page):
    """Search the NeoDB catalog with an exact book category filter."""
    if media_type != MediaTypes.BOOK.value:
        msg = f"Unsupported NeoDB media type: {media_type}"
        raise ValueError(msg)

    cache_key = f"search_neodb_{SEARCH_CACHE_VERSION}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        response = services.api_request(
            Sources.NEODB.value,
            "GET",
            f"{BASE_URL}/api/catalog/search",
            params={
                "query": query,
                "category": MediaTypes.BOOK.value,
                "page": page,
            },
            headers={"User-Agent": USER_AGENT},
            retry_rate_limit=False,
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.NEODB.value, error) from error

    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        _raise_schema_error("data must be a list")

    count = response.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        _raise_schema_error("count must be a non-negative integer")

    results = [_validate_search_item(item) for item in response["data"]]
    data = helpers.format_search_response(
        page,
        SEARCH_PAGE_SIZE,
        count,
        results,
    )
    cache.set(cache_key, data)
    return data


def book(media_id):
    """Return normalized NeoDB metadata for one book."""
    canonical_id = canonical_uuid(media_id)

    cache_key = f"neodb_{MediaTypes.BOOK.value}_{canonical_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        response = services.api_request(
            Sources.NEODB.value,
            "GET",
            f"{BASE_URL}/api/book/{canonical_id}",
            headers={"User-Agent": USER_AGENT},
            retry_rate_limit=False,
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.NEODB.value, error) from error

    if not isinstance(response, dict):
        _raise_schema_error("expected an object")

    response_uuid = response.get("uuid")
    if not isinstance(response_uuid, str) or response_uuid.strip() != canonical_id:
        _raise_schema_error("uuid must match the request")
    _validate_book_category(response)

    title = localized_title(response)
    score, score_count = _rating(response)
    genres = _string_list(response, "tags") or None
    pages = positive_int(response.get("pages"))
    authors = _string_list(response, "author")
    publisher = _optional_string(response, "pub_house")
    isbn = _optional_string(response, "isbn")

    data = {
        "media_id": canonical_id,
        "source": Sources.NEODB.value,
        "source_url": f"{BASE_URL}/book/{canonical_id}",
        "media_type": MediaTypes.BOOK.value,
        "title": title,
        "max_progress": pages,
        "image": _item_image(response),
        "synopsis": _optional_string(response, "description")
        or _optional_string(response, "brief")
        or "No synopsis available.",
        "genres": genres,
        "score": score,
        "score_count": score_count,
        "details": {
            "number_of_pages": pages,
            "author": ", ".join(authors) if authors else None,
            "publisher": publisher,
            "isbn": isbn,
            "publish_date": _publish_date(response),
        },
    }
    cache.set(cache_key, data)
    return data
