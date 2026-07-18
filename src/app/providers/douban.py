"""Douban provider for Chinese book search and metadata.

Douban has no official public API. Search uses the suggest endpoint and
metadata parses the public subject page, so both surfaces are treated as
fragile: any anti-bot interstitial or layout change raises an explicit
provider error instead of degrading silently.
"""

import json
import re

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

BASE_URL = "https://book.douban.com"
# Browser-class User-Agent: the suggest endpoint and subject pages are not an
# API program with a client-identification contract.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SEARCH_CACHE_VERSION = "v1"
BOOK_SUGGEST_TYPE = "b"
INFO_LABELS = {
    "publisher": "出版社",
    "publish_date": "出版年",
    "pages": "页数",
    "isbn": "ISBN",
    "author": "作者",
}


def _raise_schema_error(details):
    """Raise a provider error for an unexpected Douban response."""
    message = f"Invalid Douban response: {details}"
    error = ValueError(message)
    raise services.ProviderAPIError(
        Sources.DOUBAN.value,
        error,
        message,
    ) from error


def canonical_subject_id(media_id):
    """Return a validated numeric Douban subject ID string."""
    if isinstance(media_id, str):
        media_id = media_id.strip()
    if not isinstance(media_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", media_id):
        msg = f"Invalid Douban subject ID: {media_id}"
        raise ValueError(msg)
    return media_id


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


def _format_suggest_entry(entry):
    """Format one validated suggest entry as a Yamtrack search result."""
    subject_id = entry.get("id")
    if not isinstance(subject_id, str) or not re.fullmatch(r"[0-9]+", subject_id):
        _raise_schema_error("suggest id must be a numeric string")

    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        _raise_schema_error("suggest title must be a non-empty string")

    image = entry.get("pic")
    if not isinstance(image, str) or not image.strip():
        image = settings.IMG_NONE

    return {
        "media_id": subject_id,
        "source": Sources.DOUBAN.value,
        "media_type": MediaTypes.BOOK.value,
        "title": title.strip(),
        "image": image.strip() if image != settings.IMG_NONE else image,
    }


def search(media_type, query, page):
    """Search Douban books through the suggest endpoint (single page)."""
    if media_type != MediaTypes.BOOK.value:
        msg = f"Unsupported Douban media type: {media_type}"
        raise ValueError(msg)

    cache_key = f"search_douban_{SEARCH_CACHE_VERSION}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        response = services.api_request(
            Sources.DOUBAN.value,
            "GET",
            f"{BASE_URL}/j/subject_suggest",
            params={"q": query},
            headers={"User-Agent": USER_AGENT},
            retry_rate_limit=False,
        )
    except requests.RequestException as error:
        # A non-JSON body means an anti-bot interstitial, not an empty result.
        raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error

    if not isinstance(response, list):
        _raise_schema_error("suggest response must be a list")

    results = [
        _format_suggest_entry(entry)
        for entry in response
        if isinstance(entry, dict) and entry.get("type") == BOOK_SUGGEST_TYPE
    ]
    # The suggest endpoint has no pagination; report everything as one page.
    if page != 1:
        results = []
    data = helpers.format_search_response(
        page,
        max(len(results), 1),
        len(results),
        results,
    )
    cache.set(cache_key, data)
    return data


def _load_ld_json(soup):
    """Return the embedded structured-data object, or an empty dict."""
    script = soup.find("script", type="application/ld+json")
    if script is None or not script.string:
        return {}
    try:
        parsed = json.loads(script.string, strict=False)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ld_title(ld_data):
    """Return the structured-data title."""
    title = ld_data.get("name")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _meta_content(soup, prop):
    """Return a trimmed meta tag content value."""
    tag = soup.find("meta", property=prop)
    if tag is None:
        return None
    content = tag.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _ld_authors(ld_data):
    """Return structured-data author names."""
    authors = ld_data.get("author")
    if not isinstance(authors, list):
        return []
    names = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _ld_rating(ld_data):
    """Return structured-data score fields."""
    rating = ld_data.get("aggregateRating")
    if not isinstance(rating, dict):
        return None, None

    score = rating.get("ratingValue")
    if isinstance(score, str):
        try:
            score = float(score)
        except ValueError:
            score = None
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None

    score_count = rating.get("ratingCount")
    if isinstance(score_count, str):
        try:
            score_count = int(score_count)
        except ValueError:
            score_count = None
    if (
        isinstance(score_count, bool)
        or not isinstance(score_count, int)
        or score_count < 0
    ):
        score_count = None
    return score, score_count


def _info_field(info_text, label):
    """Return one labeled value from the subject info panel."""
    match = re.search(rf"{re.escape(label)}\s*[::]\s*(.+)", info_text)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _synopsis(soup):
    """Return the subject introduction, preferring the full intro block."""
    for intro in soup.select("#link-report .intro, .related_info .intro"):
        text = intro.get_text("\n", strip=True)
        if text:
            return text
    description = _meta_content(soup, "og:description")
    return description or "No synopsis available."


def _cover_image(soup):
    """Return the subject cover, falling back to the shared placeholder."""
    image = _meta_content(soup, "og:image")
    return image or settings.IMG_NONE


def book(media_id):
    """Return normalized Douban metadata for one book subject page."""
    canonical_id = canonical_subject_id(media_id)

    cache_key = f"douban_{MediaTypes.BOOK.value}_{canonical_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    try:
        page_html = services.api_request(
            Sources.DOUBAN.value,
            "GET",
            f"{BASE_URL}/subject/{canonical_id}/",
            headers={"User-Agent": USER_AGENT},
            response_format="text",
            retry_rate_limit=False,
        )
    except requests.RequestException as error:
        raise services.ProviderAPIError(Sources.DOUBAN.value, error) from error

    soup = BeautifulSoup(page_html, "html.parser")
    ld_data = _load_ld_json(soup)

    title = _ld_title(ld_data) or _meta_content(soup, "og:title")
    if not title:
        # Login walls and anti-bot interstitials have no subject markup.
        _raise_schema_error("subject page has no title markup")

    info_node = soup.find(id="info")
    info_text = info_node.get_text("\n", strip=True) if info_node else ""

    pages = positive_int(_info_field(info_text, INFO_LABELS["pages"]))
    authors = _ld_authors(ld_data)
    author = ", ".join(authors) or _info_field(info_text, INFO_LABELS["author"])
    isbn = ld_data.get("isbn")
    if not isinstance(isbn, str) or not isbn.strip():
        isbn = _info_field(info_text, INFO_LABELS["isbn"])
    else:
        isbn = isbn.strip()
    score, score_count = _ld_rating(ld_data)

    data = {
        "media_id": canonical_id,
        "source": Sources.DOUBAN.value,
        "source_url": f"{BASE_URL}/subject/{canonical_id}/",
        "media_type": MediaTypes.BOOK.value,
        "title": title,
        "max_progress": pages,
        "image": _cover_image(soup),
        "synopsis": _synopsis(soup),
        "genres": None,
        "score": score,
        "score_count": score_count,
        "details": {
            "number_of_pages": pages,
            "author": author,
            "publisher": _info_field(info_text, INFO_LABELS["publisher"]),
            "isbn": isbn,
            "publish_date": _info_field(info_text, INFO_LABELS["publish_date"]),
        },
    }
    cache.set(cache_key, data)
    return data
