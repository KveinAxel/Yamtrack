import json
from pathlib import Path
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import douban
from app.providers.services import ProviderAPIError

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


def load_json_fixture(name):
    """Load a sanitized Douban JSON fixture."""
    with (mock_path / name).open(encoding="utf-8") as fixture:
        return json.load(fixture)


def load_html_fixture(name):
    """Load a sanitized Douban page fixture."""
    with (mock_path / name).open(encoding="utf-8") as fixture:
        return fixture.read()


class DoubanSourceTests(TestCase):
    """Test the Douban source choice."""

    def test_source_choice(self):
        """Douban has the expected stored value and label."""
        self.assertEqual(Sources.DOUBAN.value, "douban")
        self.assertEqual(Sources.DOUBAN.label, "Douban")


class DoubanIdTests(TestCase):
    """Test Douban subject ID validation."""

    def test_accepts_numeric_ids(self):
        """Valid numeric subject IDs are trimmed and preserved."""
        self.assertEqual(douban.canonical_subject_id(" 38409776 "), "38409776")

    def test_rejects_invalid_ids(self):
        """Non-numeric or path-breaking IDs are rejected before any request."""
        for media_id in ("", "0", "01", "a/b", "38409776/../x", 38409776, None):
            with self.subTest(media_id=media_id), self.assertRaises(ValueError):
                douban.canonical_subject_id(media_id)


class DoubanSearchTests(TestCase):
    """Test Douban suggest search."""

    def setUp(self):
        """Clear cached provider responses."""
        cache.clear()

    def test_search_rejects_unsupported_media_types(self):
        """Only books are searchable through Douban."""
        with self.assertRaises(ValueError):
            douban.search(MediaTypes.ANIME.value, "缠斗", 1)

    @patch("app.providers.services.api_request")
    def test_search_keeps_only_book_entries(self, mock_request):
        """Suggest entries of other types are filtered out, not passed through."""
        mock_request.return_value = load_json_fixture("douban_suggest.json")

        response = douban.search(MediaTypes.BOOK.value, "缠斗", 1)

        self.assertEqual(response["page"], 1)
        self.assertEqual(response["total_results"], 1)
        self.assertEqual(response["total_pages"], 1)
        entry = response["results"][0]
        self.assertEqual(entry["media_id"], "38409776")
        self.assertEqual(entry["source"], Sources.DOUBAN.value)
        self.assertEqual(entry["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(entry["title"], "缠斗")
        self.assertTrue(entry["image"].startswith("https://"))

        request_call = mock_request.call_args
        self.assertEqual(request_call.args[:2], (Sources.DOUBAN.value, "GET"))
        self.assertEqual(request_call.kwargs["params"], {"q": "缠斗"})

    @patch("app.providers.services.api_request")
    def test_search_reports_a_single_page(self, mock_request):
        """Pages beyond the first are empty because suggest has no pagination."""
        mock_request.return_value = load_json_fixture("douban_suggest.json")

        response = douban.search(MediaTypes.BOOK.value, "缠斗", 2)

        self.assertEqual(response["results"], [])
        self.assertEqual(response["total_pages"], 0)

    @patch("app.providers.services.api_request")
    def test_search_missing_picture_uses_placeholder(self, mock_request):
        """A suggest entry without a picture uses the shared placeholder."""
        fixture = load_json_fixture("douban_suggest.json")
        fixture[0].pop("pic")
        mock_request.return_value = fixture

        response = douban.search(MediaTypes.BOOK.value, "缠斗", 1)

        self.assertEqual(response["results"][0]["image"], settings.IMG_NONE)

    @patch("app.providers.services.api_request")
    def test_search_rejects_malformed_entries(self, mock_request):
        """Malformed suggest payloads raise Douban provider errors."""
        malformed = [
            {"not": "a list"},
            [{"type": "b", "id": "not-numeric", "title": "缠斗"}],
            [{"type": "b", "id": "38409776", "title": " "}],
        ]
        for response in malformed:
            with self.subTest(response=response):
                cache.clear()
                mock_request.return_value = response
                with self.assertRaises(ProviderAPIError):
                    douban.search(MediaTypes.BOOK.value, "缠斗", 1)

    @patch("app.providers.services.api_request")
    def test_search_wraps_anti_bot_responses(self, mock_request):
        """A non-JSON interstitial surfaces as a Douban provider error."""
        mock_request.side_effect = requests.exceptions.JSONDecodeError(
            "Expecting value",
            "<html>登录</html>",
            0,
        )

        with self.assertRaises(ProviderAPIError):
            douban.search(MediaTypes.BOOK.value, "缠斗", 1)


class DoubanBookTests(TestCase):
    """Test Douban subject page metadata."""

    def setUp(self):
        """Clear cached provider responses."""
        cache.clear()

    @patch("app.providers.services.api_request")
    def test_book_maps_full_metadata(self, mock_request):
        """A complete subject page maps every approved field."""
        mock_request.return_value = load_html_fixture("douban_book.html")

        data = douban.book("38409776")

        self.assertEqual(data["media_id"], "38409776")
        self.assertEqual(data["source"], Sources.DOUBAN.value)
        self.assertEqual(
            data["source_url"],
            "https://book.douban.com/subject/38409776/",
        )
        self.assertEqual(data["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(data["title"], "缠斗")
        self.assertEqual(data["max_progress"], 420)
        self.assertTrue(data["image"].startswith("https://"))
        self.assertNotEqual(data["synopsis"], "No synopsis available.")
        self.assertEqual(data["details"]["number_of_pages"], 420)
        self.assertEqual(data["details"]["author"], "翟东升, 朱煜")
        self.assertEqual(data["details"]["publisher"], "中信出版集团")
        self.assertEqual(data["details"]["isbn"], "9787521716252")
        self.assertEqual(data["details"]["publish_date"], "2026-4-1")

        request_call = mock_request.call_args
        self.assertEqual(request_call.kwargs["response_format"], "text")

    @patch("app.providers.services.api_request")
    def test_book_without_structured_data_uses_page_fallbacks(self, mock_request):
        """Without ld+json the og and info panel values still map."""
        mock_request.return_value = load_html_fixture("douban_book_no_ld.html")

        data = douban.book("38409776")

        self.assertEqual(data["title"], "缠斗")
        self.assertEqual(data["max_progress"], 420)
        self.assertEqual(data["details"]["author"], "翟东升")
        self.assertEqual(data["details"]["isbn"], "9787521716252")
        self.assertIsNone(data["score"])
        self.assertIsNone(data["score_count"])

    @patch("app.providers.services.api_request")
    def test_book_anti_bot_page_is_a_provider_error(self, mock_request):
        """A page without subject markup raises a Douban provider error."""
        mock_request.return_value = load_html_fixture("douban_antibot.html")

        with self.assertRaises(ProviderAPIError):
            douban.book("38409776")

    @patch("app.providers.services.api_request")
    def test_book_is_cached(self, mock_request):
        """Book metadata is cached per subject."""
        mock_request.return_value = load_html_fixture("douban_book.html")

        douban.book("38409776")
        douban.book("38409776")

        self.assertEqual(mock_request.call_count, 1)

    @patch("app.providers.services.api_request")
    def test_book_wraps_network_errors(self, mock_request):
        """Transport failures surface as Douban provider errors."""
        mock_request.side_effect = requests.exceptions.ConnectionError("boom")

        with self.assertRaises(ProviderAPIError):
            douban.book("38409776")
