import json
from pathlib import Path
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import bangumi
from app.providers.services import ProviderAPIError

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


def load_fixture(name):
    """Load a sanitized Bangumi response fixture."""
    with (mock_path / name).open(encoding="utf-8") as fixture:
        return json.load(fixture)


class BangumiSourceTests(TestCase):
    """Test the Bangumi source choice."""

    def test_source_choice(self):
        """Bangumi has the expected stored value and label."""
        self.assertEqual(Sources.BANGUMI.value, "bangumi")
        self.assertEqual(Sources.BANGUMI.label, "Bangumi")


class BangumiSearchTests(TestCase):
    """Test deterministic Bangumi searches."""

    def setUp(self):
        """Clear cached search responses before each test."""
        cache.clear()

    def test_localized_title_prefers_trimmed_chinese_title(self):
        """Chinese titles are trimmed and preferred."""
        subject = {"name_cn": " 中文 ", "name": "原名"}

        self.assertEqual(bangumi.localized_title(subject), "中文")

    def test_localized_title_falls_back_to_trimmed_original_title(self):
        """Whitespace-only Chinese titles fall back to a trimmed original."""
        subject = {"name_cn": "  ", "name": " 原名 "}

        self.assertEqual(bangumi.localized_title(subject), "原名")

    def test_localized_title_rejects_invalid_schema(self):
        """Non-string and empty titles raise a provider schema error."""
        invalid_subjects = [
            {"name_cn": None, "name": 123},
            {"name_cn": "  ", "name": "  "},
        ]

        for subject in invalid_subjects:
            with self.subTest(subject=subject):
                with self.assertRaises(ProviderAPIError) as context:
                    bangumi.localized_title(subject)

                self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
                self.assertIn("invalid subject schema", str(context.exception))
                self.assertIn("name_cn", str(context.exception))
                self.assertIn("name", str(context.exception))

    @patch("app.providers.bangumi.services.api_request")
    def test_anime_search_prefers_chinese_title_and_identifies_client(
        self,
        mock_api_request,
    ):
        """Anime search uses a Chinese title and the exact identifying request."""
        mock_api_request.return_value = load_fixture("bangumi_search_anime.json")

        response = bangumi.search(MediaTypes.ANIME.value, "葬送的芙莉莲", 1)

        self.assertEqual(response["results"][0]["title"], "葬送的芙莉莲")
        self.assertEqual(response["results"][0]["source"], "bangumi")
        mock_api_request.assert_called_once_with(
            Sources.BANGUMI.value,
            "POST",
            "https://api.bgm.tv/v0/search/subjects",
            params={
                "keyword": "葬送的芙莉莲",
                "sort": "match",
                "filter": {"type": [2]},
                "limit": settings.PER_PAGE,
                "offset": 0,
            },
            headers={
                "User-Agent": (
                    "KveinAxel-Yamtrack/0.1 "
                    "(https://github.com/KveinAxel/Yamtrack)"
                ),
            },
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_supported_media_types_use_exact_type_filter(
        self,
        mock_api_request,
    ):
        """Anime, game, and book searches use their one exact subject type."""
        cases = [
            (MediaTypes.ANIME.value, "bangumi_search_anime.json", 2),
            (MediaTypes.GAME.value, "bangumi_search_game.json", 4),
            (MediaTypes.BOOK.value, "bangumi_search_book.json", 1),
        ]

        for media_type, fixture_name, subject_type in cases:
            with self.subTest(media_type=media_type):
                cache.clear()
                mock_api_request.reset_mock()
                mock_api_request.return_value = load_fixture(fixture_name)

                response = bangumi.search(media_type, "query", 1)

                self.assertEqual(response["results"][0]["media_type"], media_type)
                _, kwargs = mock_api_request.call_args
                self.assertEqual(kwargs["params"]["filter"], {"type": [subject_type]})
                self.assertEqual(kwargs["params"]["sort"], "match")
                self.assertEqual(kwargs["headers"], {"User-Agent": bangumi.USER_AGENT})

    @patch("app.providers.bangumi.services.api_request")
    def test_search_falls_back_to_original_title(self, mock_api_request):
        """Search results fall back to an original title when needed."""
        mock_api_request.return_value = load_fixture("bangumi_search_fallback.json")

        response = bangumi.search(MediaTypes.ANIME.value, "Original Title", 1)

        self.assertEqual(response["results"][0]["title"], "Original Title")

    @patch("app.providers.bangumi.services.api_request")
    def test_search_formats_pagination_and_result_fields(self, mock_api_request):
        """Search returns Yamtrack fields and derives the requested offset."""
        fixture = load_fixture("bangumi_search_game.json")
        fixture["total"] = 49
        fixture["offset"] = settings.PER_PAGE
        mock_api_request.return_value = fixture

        response = bangumi.search(MediaTypes.GAME.value, "示例冒险", 2)

        self.assertEqual(
            response,
            {
                "page": 2,
                "total_results": 49,
                "total_pages": 3,
                "results": [
                    {
                        "media_id": 900001,
                        "source": Sources.BANGUMI.value,
                        "media_type": MediaTypes.GAME.value,
                        "title": "示例冒险",
                        "image": "https://example.invalid/bangumi/game-900001.jpg",
                    },
                ],
            },
        )
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["params"]["limit"], settings.PER_PAGE)
        self.assertEqual(kwargs["params"]["offset"], settings.PER_PAGE)

    @patch("app.providers.bangumi.services.api_request")
    def test_invalid_media_type_fails_before_http_request(self, mock_api_request):
        """Unsupported types fail before any external request."""
        with self.assertRaises(ValueError):
            bangumi.search(MediaTypes.MOVIE.value, "query", 1)

        mock_api_request.assert_not_called()

    @patch("app.providers.bangumi.services.api_request")
    def test_search_normalizes_request_errors(self, mock_api_request):
        """Transport failures become chained Bangumi provider errors."""
        request_error = requests.exceptions.HTTPError("Bangumi unavailable")
        mock_api_request.side_effect = request_error

        with self.assertRaises(ProviderAPIError) as context:
            bangumi.search(MediaTypes.ANIME.value, "query", 1)

        self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
        self.assertIs(context.exception.__cause__, request_error)
        self.assertIs(context.exception.__context__, request_error)

    @patch("app.providers.bangumi.cache.set")
    @patch("app.providers.bangumi.cache.get")
    @patch("app.providers.bangumi.services.api_request")
    def test_search_uses_exact_cache_key(
        self,
        mock_api_request,
        mock_cache_get,
        mock_cache_set,
    ):
        """Cached results use the required deterministic cache key."""
        cached_response = {"results": ["cached"]}
        mock_cache_get.return_value = cached_response

        response = bangumi.search(MediaTypes.BOOK.value, "中文 查询", 3)

        self.assertIs(response, cached_response)
        mock_cache_get.assert_called_once_with("search_bangumi_book_中文 查询_3")
        mock_cache_set.assert_not_called()
        mock_api_request.assert_not_called()

    @patch("app.providers.bangumi.services.api_request")
    def test_search_caches_formatted_response(self, mock_api_request):
        """A formatted search response is cached under the exact key."""
        mock_api_request.return_value = load_fixture("bangumi_search_book.json")

        with patch("app.providers.bangumi.cache.set") as mock_cache_set:
            response = bangumi.search(MediaTypes.BOOK.value, "示例图书", 1)

        mock_cache_set.assert_called_once_with(
            "search_bangumi_book_示例图书_1",
            response,
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_search_rejects_invalid_response_schema(self, mock_api_request):
        """Invalid result envelopes raise provider schema errors."""
        valid = load_fixture("bangumi_search_anime.json")
        invalid_responses = [
            {**valid, "data": {}},
            {**valid, "total": -1},
            {**valid, "total": True},
            {**valid, "limit": -1},
            {**valid, "limit": "24"},
            {**valid, "offset": -1},
            {**valid, "offset": "0"},
            {**valid, "limit": settings.PER_PAGE + 1},
            {**valid, "offset": 1},
        ]

        for index, response in enumerate(invalid_responses):
            with self.subTest(response=response):
                cache.clear()
                mock_api_request.return_value = response

                with self.assertRaises(ProviderAPIError) as context:
                    bangumi.search(MediaTypes.ANIME.value, f"query-{index}", 1)

                self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
                self.assertIn("invalid search response schema", str(context.exception))

    @patch("app.providers.bangumi.services.api_request")
    def test_search_normalizes_invalid_subject_schema(self, mock_api_request):
        """Every invalid required subject field raises a provider schema error."""
        valid_subject = load_fixture("bangumi_search_anime.json")["data"][0]
        invalid_subjects = [
            ("not an object", None, "object"),
            ("missing id", {k: v for k, v in valid_subject.items() if k != "id"}, "id"),
            ("boolean id", {**valid_subject, "id": True}, "id"),
            ("zero id", {**valid_subject, "id": 0}, "id"),
            ("negative id", {**valid_subject, "id": -1}, "id"),
            (
                "missing type",
                {k: v for k, v in valid_subject.items() if k != "type"},
                "type",
            ),
            ("boolean type", {**valid_subject, "type": True}, "type"),
            ("string type", {**valid_subject, "type": "2"}, "type"),
            ("mismatched type", {**valid_subject, "type": 4}, "type"),
            (
                "missing titles",
                {
                    k: v
                    for k, v in valid_subject.items()
                    if k not in {"name", "name_cn"}
                },
                "name",
            ),
            (
                "non-string titles",
                {**valid_subject, "name": 123, "name_cn": None},
                "name",
            ),
            (
                "blank titles",
                {**valid_subject, "name": "  ", "name_cn": "\t"},
                "name",
            ),
            (
                "missing images",
                {k: v for k, v in valid_subject.items() if k != "images"},
                "images",
            ),
            ("non-object images", {**valid_subject, "images": None}, "images"),
            ("missing large image", {**valid_subject, "images": {}}, "images.large"),
            (
                "non-string large image",
                {**valid_subject, "images": {"large": None}},
                "images.large",
            ),
            (
                "blank large image",
                {**valid_subject, "images": {"large": "  "}},
                "images.large",
            ),
        ]

        for index, (label, subject, expected_detail) in enumerate(invalid_subjects):
            with self.subTest(label=label):
                cache.clear()
                response = load_fixture("bangumi_search_anime.json")
                response["data"] = [subject]
                mock_api_request.return_value = response

                with self.assertRaises(ProviderAPIError) as context:
                    bangumi.search(MediaTypes.ANIME.value, f"invalid-{index}", 1)

                self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
                self.assertIn("invalid subject schema", str(context.exception))
                self.assertIn(expected_detail, str(context.exception))

    @patch("app.providers.bangumi.services.api_request")
    def test_search_rejects_invalid_subject_instead_of_skipping_it(
        self,
        mock_api_request,
    ):
        """An invalid subject raises rather than silently dropping an entry."""
        fixture = load_fixture("bangumi_search_anime.json")
        fixture["data"].append(
            {
                "id": 900004,
                "type": 2,
                "name": None,
                "name_cn": None,
                "summary": "Invalid title schema.",
                "images": {"large": "https://example.invalid/invalid.jpg"},
            },
        )
        fixture["total"] = 2
        mock_api_request.return_value = fixture

        with self.assertRaises(ProviderAPIError):
            bangumi.search(MediaTypes.ANIME.value, "query", 1)
