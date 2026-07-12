"""Bangumi provider for Chinese-first media search."""

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

BASE_URL = "https://api.bgm.tv/v0"
USER_AGENT = "KveinAxel-Yamtrack/0.1 (https://github.com/KveinAxel/Yamtrack)"
EPISODE_PAGE_LIMIT = 200
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


def _normalize_infobox_value(value):
    """Return normalized values and any labels carried by value objects."""
    if isinstance(value, str) and value.strip():
        return [value.strip()], []
    if not isinstance(value, list):
        return [], []

    values = []
    keyed_values = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_value = item.get("v")
        if not isinstance(item_value, str) or not item_value.strip():
            continue
        item_value = item_value.strip()
        values.append(item_value)
        item_key = item.get("k")
        if isinstance(item_key, str) and item_key.strip():
            keyed_values.append((item_key.strip(), item_value))
    return values, keyed_values


def normalize_infobox(subject):
    """Normalize optional Bangumi infobox entries into string lists."""
    if not isinstance(subject, dict) or not isinstance(subject.get("infobox"), list):
        return {}

    info = {}
    for entry in subject["infobox"]:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            continue

        values, keyed_values = _normalize_infobox_value(entry.get("value"))
        if values:
            info.setdefault(key.strip(), []).extend(values)
        for item_key, item_value in keyed_values:
            info.setdefault(item_key, []).append(item_value)
    return info


def first_infobox_value(info, *keys):
    """Return the first non-empty normalized value for the requested aliases."""
    if not isinstance(info, dict):
        return None
    for key in keys:
        values = info.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


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


def _raise_detail_schema_error(details):
    """Raise a provider error for an invalid Bangumi detail response."""
    message = f"Invalid Bangumi response: {details}"
    error = ValueError(message)
    raise services.ProviderAPIError(
        Sources.BANGUMI.value,
        error,
        message,
    ) from error


def _validate_detail_subject(response, media_id, subject_type):
    """Validate the required identity and title of a detail response."""
    if not isinstance(response, dict):
        _raise_detail_schema_error("expected an object")

    response_id = response.get("id")
    if (
        isinstance(response_id, bool)
        or not isinstance(response_id, int)
        or response_id <= 0
        or response_id != media_id
    ):
        _raise_detail_schema_error("id must be a matching positive integer")

    response_type = response.get("type")
    if (
        isinstance(response_type, bool)
        or not isinstance(response_type, int)
        or response_type != subject_type
    ):
        _raise_detail_schema_error("type must match the request")

    for field in ("name_cn", "name"):
        value = response.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _raise_detail_schema_error("title must be a non-empty string")


def _detail_image(response):
    """Return the preferred usable Bangumi detail image."""
    images = response.get("images")
    if isinstance(images, dict):
        for key in ("large", "common"):
            image = images.get(key)
            if isinstance(image, str) and image.strip():
                return image.strip()
    return settings.IMG_NONE


def _detail_rating(response):
    """Return correctly typed Bangumi score fields."""
    rating = response.get("rating")
    if not isinstance(rating, dict):
        return None, None

    score = rating.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None

    score_count = rating.get("total")
    if (
        isinstance(score_count, bool)
        or not isinstance(score_count, int)
        or score_count < 0
    ):
        score_count = None
    return score, score_count


def _detail_tags(response):
    """Return correctly typed Bangumi tag names."""
    tags = response.get("tags")
    if not isinstance(tags, list):
        return None
    names = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = tag.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names or None


def _optional_string(response, key):
    """Return a trimmed optional string."""
    value = response.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _game_details(response, info):
    """Map approved Bangumi game details."""
    platforms = info.get("平台")
    if not isinstance(platforms, list) or not platforms:
        platforms = None
    return {
        "release_date": _optional_string(response, "date")
        or first_infobox_value(info, "发售日", "发售日期", "发行日期"),
        "platforms": platforms,
        "format": first_infobox_value(info, "游戏类型", "类型"),
        "developer": first_infobox_value(info, "开发商", "开发"),
        "publisher": first_infobox_value(info, "发行商", "发行"),
    }


def _book_details(response, info):
    """Map approved Bangumi book details."""
    pages = positive_int(first_infobox_value(info, "页数"))
    details = {
        "number_of_pages": pages,
        "author": first_infobox_value(info, "作者"),
        "publisher": first_infobox_value(info, "出版社"),
        "isbn": first_infobox_value(info, "ISBN", "ISBN-13"),
        "publish_date": first_infobox_value(
            info,
            "发售日",
            "出版日",
            "出版日期",
            "出版年",
        )
        or _optional_string(response, "date"),
    }
    return pages, details


def _validate_episode_data(page_data, limit):
    """Validate episode row containers without retaining their contents."""
    if not isinstance(page_data, list):
        _raise_detail_schema_error("episode data must be a list")
    if any(not isinstance(item, dict) for item in page_data):
        _raise_detail_schema_error("episode data items must be objects")
    if len(page_data) > limit:
        _raise_detail_schema_error("episode data cannot exceed the page limit")


def _validate_episode_page(response, expected_total, expected_offset):
    """Validate one stable Bangumi ordinary-episode response page."""
    if not isinstance(response, dict):
        _raise_detail_schema_error("episode page must be an object")

    total = response.get("total")
    limit = response.get("limit")
    response_offset = response.get("offset")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        _raise_detail_schema_error("episode total must be a non-negative integer")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit != EPISODE_PAGE_LIMIT
    ):
        _raise_detail_schema_error("episode limit must match the request")
    if (
        isinstance(response_offset, bool)
        or not isinstance(response_offset, int)
        or response_offset < 0
        or response_offset != expected_offset
    ):
        _raise_detail_schema_error("episode offset must match the request")
    page_data = response.get("data")
    _validate_episode_data(page_data, limit)
    if response_offset > total or response_offset + len(page_data) > total:
        _raise_detail_schema_error("episode page exceeds its total")
    if expected_total is not None and total != expected_total:
        _raise_detail_schema_error("episode total changed during pagination")
    if total == 0 and (response_offset != 0 or page_data):
        _raise_detail_schema_error("zero-total episode page must be empty")
    if response_offset < total and not page_data:
        _raise_detail_schema_error("episode pagination stopped before completion")
    return total, page_data


def ordinary_episode_count(subject_id):
    """Count unique ordinary Bangumi episodes without retaining episode rows."""
    canonical_subject_id = positive_int(subject_id)
    if canonical_subject_id is None:
        msg = f"Invalid Bangumi subject ID: {subject_id}"
        raise ValueError(msg)

    expected_total = None
    offset = 0
    seen_ids = set()
    pages_fetched = 0
    max_pages = None

    while expected_total is None or offset < expected_total:
        if max_pages is not None and pages_fetched >= max_pages:
            _raise_detail_schema_error("episode pagination exceeded its page bound")
        try:
            response = services.api_request(
                Sources.BANGUMI.value,
                "GET",
                f"{BASE_URL}/episodes",
                params={
                    "subject_id": canonical_subject_id,
                    "type": 0,
                    "limit": EPISODE_PAGE_LIMIT,
                    "offset": offset,
                },
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as error:
            raise services.ProviderAPIError(Sources.BANGUMI.value, error) from error

        total, page_data = _validate_episode_page(response, expected_total, offset)
        pages_fetched += 1
        if expected_total is None:
            expected_total = total
            max_pages = (
                (expected_total + EPISODE_PAGE_LIMIT - 1) // EPISODE_PAGE_LIMIT
                if expected_total
                else 1
            )

        previous_offset = offset
        offset += len(page_data)
        if expected_total and offset <= previous_offset:
            _raise_detail_schema_error("episode pagination did not advance")

        for item in page_data:
            episode_id = item.get("id")
            if (
                not isinstance(episode_id, bool)
                and isinstance(episode_id, int)
                and episode_id > 0
            ):
                seen_ids.add(episode_id)

    return None if expected_total == 0 else len(seen_ids)


def _anime_details(response, info, episode_count):
    """Map approved Bangumi anime detail fields."""
    return {
        "format": first_infobox_value(info, "平台"),
        "start_date": _optional_string(response, "date"),
        "episodes": episode_count,
    }


def subject(media_id, media_type):
    """Return normalized Bangumi metadata for one subject."""
    if media_type not in SUBJECT_TYPES:
        msg = f"Unsupported Bangumi media type: {media_type}"
        raise ValueError(msg)

    requested_id = positive_int(media_id)
    if requested_id is None:
        msg = f"Invalid Bangumi subject ID: {media_id}"
        raise ValueError(msg)
    canonical_id = str(requested_id)

    cache_key = f"bangumi_{media_type}_{canonical_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        response = services.api_request(
            Sources.BANGUMI.value,
            "GET",
            f"{BASE_URL}/subjects/{canonical_id}",
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.BANGUMI.value, error) from error

    subject_type = SUBJECT_TYPES[media_type]
    title = _validate_detail_subject(response, requested_id, subject_type)
    info = normalize_infobox(response)
    score, score_count = _detail_rating(response)
    max_progress = None
    if media_type == MediaTypes.GAME.value:
        details = _game_details(response, info)
    elif media_type == MediaTypes.BOOK.value:
        max_progress, details = _book_details(response, info)
    elif media_type == MediaTypes.ANIME.value:
        max_progress = ordinary_episode_count(requested_id)
        details = _anime_details(response, info, max_progress)
    else:
        details = {}

    data = {
        "media_id": canonical_id,
        "source": Sources.BANGUMI.value,
        "source_url": f"https://bgm.tv/subject/{canonical_id}",
        "media_type": media_type,
        "title": title,
        "max_progress": max_progress,
        "image": _detail_image(response),
        "synopsis": _optional_string(response, "summary")
        or "No synopsis available.",
        "genres": _detail_tags(response),
        "score": score,
        "score_count": score_count,
        "details": details,
    }
    cache.set(cache_key, data)
    return data


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
