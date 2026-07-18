import json
from pathlib import Path
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import neodb
from app.providers.services import ProviderAPIError

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


def load_fixture(name):
    """Load a sanitized NeoDB response fixture."""
    with (mock_path / name).open(encoding="utf-8") as fixture:
        return json.load(fixture)


class NeodbSourceTests(TestCase):
    """Test the NeoDB source choice."""

    def test_source_choice(self):
        """NeoDB has the expected stored value and label."""
        self.assertEqual(Sources.NEODB.value, "neodb")
        self.assertEqual(Sources.NEODB.label, "NeoDB")


class NeodbTitleTests(TestCase):
    """Test the fixed NeoDB title policy."""

    def test_prefers_simplified_chinese_localized_title(self):
        """A non-empty zh-cn localized title wins over the display title."""
        item = {
            "localized_title": [
                {"lang": "en", "text": "Entangled"},
                {"lang": "zh-cn", "text": " 缠斗 "},
            ],
            "display_title": "Other",
        }
        self.assertEqual(neodb.localized_title(item), "缠斗")

    def test_falls_back_to_display_title_then_title(self):
        """Missing localized titles fall back to display and original titles."""
        self.assertEqual(
            neodb.localized_title({"localized_title": [], "display_title": " 缠斗 "}),
            "缠斗",
        )
        self.assertEqual(
            neodb.localized_title({"display_title": "  ", "title": "缠斗"}),
            "缠斗",
        )

    def test_rejects_items_without_any_title(self):
        """An item with no usable title is a schema error."""
        with self.assertRaises(ProviderAPIError):
            neodb.localized_title({"display_title": " ", "title": ""})

    def test_rejects_non_object_items(self):
        """A non-object item is a schema error."""
        with self.assertRaises(ProviderAPIError):
            neodb.localized_title(["not", "an", "object"])


class NeodbIdTests(TestCase):
    """Test NeoDB item ID validation."""

    def test_accepts_base62_uuid(self):
        """Valid item IDs are trimmed and preserved."""
        self.assertEqual(
            neodb.canonical_uuid(" 54lhOEeEYQP0eyJJaMdVUX "),
            "54lhOEeEYQP0eyJJaMdVUX",
        )

    def test_rejects_invalid_ids(self):
        """Path-breaking or empty IDs are rejected before any request."""
        for media_id in ("", "  ", "a/b", "../etc", 123, None):
            with self.subTest(media_id=media_id), self.assertRaises(ValueError):
                neodb.canonical_uuid(media_id)


class NeodbSearchTests(TestCase):
    """Test NeoDB catalog search."""

    def setUp(self):
        """Clear cached provider responses."""
        cache.clear()

    def test_search_rejects_unsupported_media_types(self):
        """Only books are searchable through NeoDB."""
        with self.assertRaises(ValueError):
            neodb.search(MediaTypes.ANIME.value, "缠斗", 1)

    @patch("app.providers.services.api_request")
    def test_search_formats_results(self, mock_request):
        """Search results map NeoDB items into Yamtrack search entries."""
        mock_request.return_value = load_fixture("neodb_search.json")

        response = neodb.search(MediaTypes.BOOK.value, "缠斗", 1)

        self.assertEqual(response["page"], 1)
        self.assertEqual(response["total_results"], 2)
        self.assertEqual(response["total_pages"], 1)
        first = response["results"][0]
        self.assertEqual(first["media_id"], "54lhOEeEYQP0eyJJaMdVUX")
        self.assertEqual(first["source"], Sources.NEODB.value)
        self.assertEqual(first["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(first["title"], "缠斗")

        request_call = mock_request.call_args
        self.assertEqual(
            request_call.args[:2],
            (Sources.NEODB.value, "GET"),
        )
        self.assertEqual(
            request_call.kwargs["params"],
            {"query": "缠斗", "category": "book", "page": 1},
        )

    @patch("app.providers.services.api_request")
    def test_search_caches_pages_separately(self, mock_request):
        """Each search page uses its own cache entry."""
        mock_request.return_value = load_fixture("neodb_search.json")

        neodb.search(MediaTypes.BOOK.value, "缠斗", 1)
        neodb.search(MediaTypes.BOOK.value, "缠斗", 1)
        neodb.search(MediaTypes.BOOK.value, "缠斗", 2)

        self.assertEqual(mock_request.call_count, 2)

    @patch("app.providers.services.api_request")
    def test_search_rejects_non_book_categories(self, mock_request):
        """A result of another category is a schema error, not a result."""
        fixture = load_fixture("neodb_search.json")
        fixture["data"][0]["category"] = "movie"
        mock_request.return_value = fixture

        with self.assertRaises(ProviderAPIError):
            neodb.search(MediaTypes.BOOK.value, "缠斗", 1)

    @patch("app.providers.services.api_request")
    def test_search_rejects_malformed_envelopes(self, mock_request):
        """Malformed search envelopes are schema errors."""
        malformed = [
            {"pages": 1, "count": 1},
            {"data": "not-a-list", "count": 1},
            {"data": [], "count": -1},
            {"data": [], "count": True},
            {"data": [{"category": "book", "uuid": "!bad!"}], "count": 1},
            "not-an-object",
        ]
        for response in malformed:
            with self.subTest(response=response):
                cache.clear()
                mock_request.return_value = response
                with self.assertRaises(ProviderAPIError):
                    neodb.search(MediaTypes.BOOK.value, "缠斗", 1)

    @patch("app.providers.services.api_request")
    def test_search_wraps_network_errors(self, mock_request):
        """Transport failures surface as NeoDB provider errors."""
        mock_request.side_effect = requests.exceptions.ConnectionError("boom")

        with self.assertRaises(ProviderAPIError):
            neodb.search(MediaTypes.BOOK.value, "缠斗", 1)


class NeodbBookTests(TestCase):
    """Test NeoDB book metadata."""

    def setUp(self):
        """Clear cached provider responses."""
        cache.clear()

    @patch("app.providers.services.api_request")
    def test_book_maps_full_metadata(self, mock_request):
        """A complete NeoDB edition maps every approved field."""
        mock_request.return_value = load_fixture("neodb_book.json")

        data = neodb.book("54lhOEeEYQP0eyJJaMdVUX")

        self.assertEqual(data["media_id"], "54lhOEeEYQP0eyJJaMdVUX")
        self.assertEqual(data["source"], Sources.NEODB.value)
        self.assertEqual(
            data["source_url"],
            "https://neodb.social/book/54lhOEeEYQP0eyJJaMdVUX",
        )
        self.assertEqual(data["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(data["title"], "缠斗")
        self.assertEqual(data["max_progress"], 344)
        self.assertTrue(data["image"].startswith("https://"))
        self.assertNotEqual(data["synopsis"], "No synopsis available.")
        self.assertEqual(data["score_count"], 2)
        self.assertEqual(data["details"]["number_of_pages"], 344)
        self.assertEqual(data["details"]["author"], "袁伟时")
        self.assertEqual(data["details"]["publisher"], "线装书局")
        self.assertEqual(data["details"]["isbn"], "9787512007307")
        self.assertEqual(data["details"]["publish_date"], "2013-02")

    @patch("app.providers.services.api_request")
    def test_book_missing_optional_fields_degrade(self, mock_request):
        """Missing optional fields degrade without blocking the item."""
        fixture = load_fixture("neodb_book.json")
        for key in (
            "description",
            "brief",
            "cover_image_url",
            "rating",
            "rating_count",
            "tags",
            "pages",
            "author",
            "pub_house",
            "isbn",
            "pub_year",
            "pub_month",
        ):
            fixture.pop(key, None)
        mock_request.return_value = fixture

        data = neodb.book("54lhOEeEYQP0eyJJaMdVUX")

        self.assertEqual(data["title"], "缠斗")
        self.assertIsNone(data["max_progress"])
        self.assertEqual(data["image"], settings.IMG_NONE)
        self.assertEqual(data["synopsis"], "No synopsis available.")
        self.assertIsNone(data["genres"])
        self.assertIsNone(data["score"])
        self.assertIsNone(data["score_count"])
        self.assertEqual(
            data["details"],
            {
                "number_of_pages": None,
                "author": None,
                "publisher": None,
                "isbn": None,
                "publish_date": None,
            },
        )

    @patch("app.providers.services.api_request")
    def test_book_malformed_containers_are_schema_errors(self, mock_request):
        """Malformed required containers raise NeoDB provider errors."""
        base = load_fixture("neodb_book.json")
        wrong_uuid = dict(base, uuid="mismatched")
        wrong_category = dict(base, category="movie")
        malformed = ["not-an-object", wrong_uuid, wrong_category]
        for response in malformed:
            with self.subTest(response=response):
                cache.clear()
                mock_request.return_value = response
                with self.assertRaises(ProviderAPIError):
                    neodb.book("54lhOEeEYQP0eyJJaMdVUX")

    @patch("app.providers.services.api_request")
    def test_book_year_only_publish_date(self, mock_request):
        """A missing month degrades the publish date to the year."""
        fixture = load_fixture("neodb_book.json")
        fixture["pub_month"] = None
        mock_request.return_value = fixture

        data = neodb.book("54lhOEeEYQP0eyJJaMdVUX")

        self.assertEqual(data["details"]["publish_date"], "2013")

    @patch("app.providers.services.api_request")
    def test_book_is_cached(self, mock_request):
        """Book metadata is cached per item."""
        mock_request.return_value = load_fixture("neodb_book.json")

        neodb.book("54lhOEeEYQP0eyJJaMdVUX")
        neodb.book("54lhOEeEYQP0eyJJaMdVUX")

        self.assertEqual(mock_request.call_count, 1)

    @patch("app.providers.services.api_request")
    def test_book_wraps_network_errors(self, mock_request):
        """Transport failures surface as NeoDB provider errors."""
        mock_request.side_effect = requests.exceptions.ConnectionError("boom")

        with self.assertRaises(ProviderAPIError):
            neodb.book("54lhOEeEYQP0eyJJaMdVUX")
