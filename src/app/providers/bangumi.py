"""Bangumi provider for Chinese-first media search."""

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

BASE_URL = "https://api.bgm.tv/v0"
USER_AGENT = "KveinAxel-Yamtrack/0.1 (https://github.com/KveinAxel/Yamtrack)"
SUBJECT_TYPES = {
    MediaTypes.BOOK.value: 1,
    MediaTypes.ANIME.value: 2,
    MediaTypes.GAME.value: 4,
}


def _raise_schema_error(details):
    """Raise a provider error for an unexpected Bangumi response."""
    error = ValueError(details)
    raise services.ProviderAPIError(Sources.BANGUMI.value, error, details)


def localized_title(subject):
    """Prefer a non-empty Chinese title, falling back to the original title."""
    if not isinstance(subject, dict):
        _raise_schema_error("invalid subject schema: expected an object")

    for field in ("name_cn", "name"):
        title = subject.get(field)
        if isinstance(title, str) and title.strip():
            return title.strip()

    return _raise_schema_error(
        "invalid subject schema: name_cn and name must provide a non-empty string",
    )


def _validate_pagination(response, expected_limit, expected_offset):
    """Validate the pagination envelope returned by Bangumi."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        _raise_schema_error("invalid search response schema: data must be a list")

    for field in ("total", "limit", "offset"):
        value = response.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _raise_schema_error(
                "invalid search response schema: "
                f"{field} must be a non-negative integer",
            )

    if response["limit"] != expected_limit:
        _raise_schema_error(
            "invalid search response schema: limit does not match the request",
        )
    if response["offset"] != expected_offset:
        _raise_schema_error(
            "invalid search response schema: offset does not match the request",
        )


def _validate_subject(subject, expected_type):
    """Validate and normalize required Bangumi subject fields."""
    if not isinstance(subject, dict):
        _raise_schema_error("invalid subject schema: expected an object")

    media_id = subject.get("id")
    if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0:
        _raise_schema_error("invalid subject schema: id must be a positive integer")

    subject_type = subject.get("type")
    if isinstance(subject_type, bool) or not isinstance(subject_type, int):
        _raise_schema_error("invalid subject schema: type must be an integer")
    if subject_type != expected_type:
        _raise_schema_error("invalid subject schema: type does not match the request")

    title = localized_title(subject)
    images = subject.get("images")
    if not isinstance(images, dict):
        _raise_schema_error("invalid subject schema: images must be an object")
    image = images.get("large")
    if not isinstance(image, str) or not image.strip():
        _raise_schema_error(
            "invalid subject schema: images.large must be a non-empty string",
        )

    return {"id": media_id, "title": title, "image": image.strip()}


def _format_subject(validated_subject, media_type):
    """Format validated subject values as a Yamtrack search result."""
    return {
        "media_id": validated_subject["id"],
        "source": Sources.BANGUMI.value,
        "media_type": media_type,
        "title": validated_subject["title"],
        "image": validated_subject["image"],
    }


def search(media_type, query, page):
    """Search Bangumi subjects using an exact media-type filter."""
    if media_type not in SUBJECT_TYPES:
        msg = f"Unsupported Bangumi media type: {media_type}"
        raise ValueError(msg)

    cache_key = f"search_bangumi_{media_type}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    subject_type = SUBJECT_TYPES[media_type]
    offset = (page - 1) * settings.PER_PAGE
    params = {
        "keyword": query,
        "sort": "match",
        "filter": {"type": [subject_type]},
        "limit": settings.PER_PAGE,
        "offset": offset,
    }

    try:
        response = services.api_request(
            Sources.BANGUMI.value,
            "POST",
            f"{BASE_URL}/search/subjects",
            params=params,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.BANGUMI.value, error) from error

    _validate_pagination(response, settings.PER_PAGE, offset)
    results = [
        _format_subject(_validate_subject(subject, subject_type), media_type)
        for subject in response["data"]
    ]
    data = helpers.format_search_response(
        page,
        settings.PER_PAGE,
        response["total"],
        results,
    )
    cache.set(cache_key, data)
    return data
