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


class BangumiNormalizationTests(TestCase):
    """Test pure Bangumi response normalization helpers."""

    def test_normalize_infobox_supports_scalar_and_list_values(self):
        """Scalar values and lists of value objects become string lists."""
        subject = {
            "infobox": [
                {"key": "发售日期", "value": "2020-07-17"},
                {
                    "key": "平台",
                    "value": [{"v": "PlayStation 4"}, {"v": "PC"}],
                },
            ],
        }

        self.assertEqual(
            bangumi.normalize_infobox(subject),
            {"发售日期": ["2020-07-17"], "平台": ["PlayStation 4", "PC"]},
        )

    def test_normalize_infobox_supports_keyed_value_objects(self):
        """Labels in keyed values are retained without losing their values."""
        subject = {
            "infobox": [
                {
                    "key": "开发",
                    "value": [
                        {"k": "开发商", "v": "Sucker Punch Productions"},
                        {"k": "发行商", "v": "Sony Interactive Entertainment"},
                    ],
                },
            ],
        }

        self.assertEqual(
            bangumi.normalize_infobox(subject),
            {
                "开发": [
                    "Sucker Punch Productions",
                    "Sony Interactive Entertainment",
                ],
                "开发商": ["Sucker Punch Productions"],
                "发行商": ["Sony Interactive Entertainment"],
            },
        )

    def test_normalize_infobox_ignores_missing_and_malformed_values(self):
        """Malformed optional infobox containers and entries degrade to no values."""
        malformed_subjects = [
            {},
            {"infobox": None},
            {"infobox": {}},
            {"infobox": [None, "bad", {}, {"key": "平台"}]},
            {
                "infobox": [
                    {"key": "平台", "value": [None, {}, {"v": 3}, "PC"]},
                    {"key": 4, "value": "ignored"},
                ],
            },
        ]

        for subject in malformed_subjects:
            with self.subTest(subject=subject):
                self.assertEqual(bangumi.normalize_infobox(subject), {})

    def test_first_infobox_value_uses_first_present_key(self):
        """The first usable value for the first matching alias is returned."""
        info = {"开发": [], "开发商": [" Studio ", "Other"]}

        self.assertEqual(
            bangumi.first_infobox_value(info, "开发", "开发商"),
            "Studio",
        )
        self.assertIsNone(bangumi.first_infobox_value(info, "发行", "出版社"))

    def test_positive_int_accepts_only_strictly_positive_whole_numbers(self):
        """Page counts accept only strictly positive whole numbers."""
        self.assertEqual(bangumi.positive_int(302), 302)
        self.assertEqual(bangumi.positive_int("302"), 302)
        for value in (0, -1, 1.5, True, False, "1.5", "not a number", None):
            with self.subTest(value=value):
                self.assertIsNone(bangumi.positive_int(value))


class BangumiDetailTests(TestCase):
    """Test common Bangumi game and book detail mapping."""

    def setUp(self):
        """Clear detail responses before each test."""
        cache.clear()

    @patch("app.providers.bangumi.services.api_request")
    def test_game_maps_approved_common_and_detail_fields(self, mock_api_request):
        """A game maps only the approved Yamtrack detail schema."""
        mock_api_request.return_value = load_fixture("bangumi_game.json")

        response = bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertEqual(
            response,
            {
                "media_id": "313495",
                "source": Sources.BANGUMI.value,
                "source_url": "https://bgm.tv/subject/313495",
                "media_type": MediaTypes.GAME.value,
                "title": "对马岛之魂",
                "max_progress": None,
                "image": "https://example.invalid/bangumi/game-313495-large.jpg",
                "synopsis": (
                    "在战火中的对马岛,武士境井仁必须开辟新的道路守护家园。"
                ),
                "genres": ["开放世界", "动作"],
                "score": 7.6,
                "score_count": 1284,
                "details": {
                    "release_date": "2020-07-17",
                    "platforms": ["PlayStation 4", "PlayStation 5"],
                    "format": "动作冒险",
                    "developer": "Sucker Punch Productions",
                    "publisher": "Sony Interactive Entertainment",
                },
            },
        )
        mock_api_request.assert_called_once_with(
            Sources.BANGUMI.value,
            "GET",
            "https://api.bgm.tv/v0/subjects/313495",
            headers={"User-Agent": bangumi.USER_AGENT},
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_book_maps_pages_and_bibliographic_details(self, mock_api_request):
        """Book progress comes from normalized pages, never subject episodes."""
        mock_api_request.return_value = load_fixture("bangumi_book.json")

        response = bangumi.subject(9585, MediaTypes.BOOK.value)

        self.assertEqual(
            response,
            {
                "media_id": "9585",
                "source": Sources.BANGUMI.value,
                "source_url": "https://bgm.tv/subject/9585",
                "media_type": MediaTypes.BOOK.value,
                "title": "三体",
                "max_progress": 302,
                "image": "https://example.invalid/bangumi/book-9585-large.jpg",
                "synopsis": load_fixture("bangumi_book.json")["summary"],
                "genres": ["科幻"],
                "score": 8.7,
                "score_count": 7654,
                "details": {
                    "number_of_pages": 302,
                    "author": "刘慈欣",
                    "publisher": "重庆出版社",
                    "isbn": "9787536692930",
                    "publish_date": "2008-01-01",
                },
            },
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_book_publish_date_uses_real_infobox_sale_date(self, mock_api_request):
        """The real Bangumi sale-date key works without a usable top-level date."""
        fixture = load_fixture("bangumi_book.json")
        fixture["date"] = None
        mock_api_request.return_value = fixture

        response = bangumi.subject(9585, MediaTypes.BOOK.value)

        self.assertEqual(response["details"]["publish_date"], "2008-01-01")

    @patch("app.providers.bangumi.cache.set")
    @patch("app.providers.bangumi.cache.get")
    @patch("app.providers.bangumi.services.api_request")
    def test_detail_canonicalizes_valid_request_id(
        self,
        mock_api_request,
        mock_cache_get,
        mock_cache_set,
    ):
        """Equivalent positive IDs share canonical URL, cache, and output identity."""
        mock_cache_get.return_value = None
        mock_api_request.return_value = load_fixture("bangumi_book.json")

        response = bangumi.subject("0009585", MediaTypes.BOOK.value)

        self.assertEqual(response["media_id"], "9585")
        self.assertEqual(response["source_url"], "https://bgm.tv/subject/9585")
        mock_cache_get.assert_called_once_with("bangumi_book_9585")
        mock_cache_set.assert_called_once_with("bangumi_book_9585", response)
        mock_api_request.assert_called_once_with(
            Sources.BANGUMI.value,
            "GET",
            "https://api.bgm.tv/v0/subjects/9585",
            headers={"User-Agent": bangumi.USER_AGENT},
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_book_rejects_invalid_page_counts(self, mock_api_request):
        """Invalid page values cannot become book progress."""
        for page_value in ("0", "-1", "1.5", True, "many"):
            with self.subTest(page_value=page_value):
                cache.clear()
                fixture = load_fixture("bangumi_book.json")
                fixture["infobox"][0]["value"] = page_value
                mock_api_request.return_value = fixture

                response = bangumi.subject(9585, MediaTypes.BOOK.value)

                self.assertIsNone(response["max_progress"])
                self.assertIsNone(response["details"]["number_of_pages"])

    @patch("app.providers.bangumi.services.api_request")
    def test_image_prefers_large_then_common_then_placeholder(self, mock_api_request):
        """Detail images follow the required deterministic fallback order."""
        common_fixture = load_fixture("bangumi_game.json")
        common_fixture["images"]["large"] = " "
        missing_fixture = load_fixture("bangumi_missing_optional.json")
        mock_api_request.side_effect = [common_fixture, missing_fixture]

        common = bangumi.subject(313495, MediaTypes.GAME.value)
        missing = bangumi.subject(42, MediaTypes.GAME.value)

        self.assertEqual(
            common["image"],
            "https://example.invalid/bangumi/game-313495-common.jpg",
        )
        self.assertEqual(missing["image"], settings.IMG_NONE)

    @patch("app.providers.bangumi.services.api_request")
    def test_malformed_optional_fields_degrade_independently(self, mock_api_request):
        """Malformed optional values do not invalidate required fields."""
        fixture = load_fixture("bangumi_missing_optional.json")
        fixture.update(
            {
                "summary": ["bad"],
                "images": ["bad"],
                "rating": {"score": True, "total": "12"},
                "tags": {"name": "bad"},
                "date": 2020,
                "infobox": {"key": "bad"},
            },
        )
        mock_api_request.return_value = fixture

        response = bangumi.subject(42, MediaTypes.GAME.value)

        self.assertEqual(response["title"], "缺少可选字段的游戏")
        self.assertEqual(response["image"], settings.IMG_NONE)
        self.assertEqual(response["synopsis"], "No synopsis available.")
        self.assertIsNone(response["genres"])
        self.assertIsNone(response["score"])
        self.assertIsNone(response["score_count"])
        self.assertEqual(
            response["details"],
            {
                "release_date": None,
                "platforms": None,
                "format": None,
                "developer": None,
                "publisher": None,
            },
        )

    @patch("app.providers.bangumi.services.api_request")
    def test_detail_validates_required_id_type_and_title(self, mock_api_request):
        """Required response identity and title must be valid and match the request."""
        malformed = load_fixture("bangumi_malformed.json")
        game = load_fixture("bangumi_game.json")
        invalid_subjects = [
            malformed,
            {**game, "id": 313496},
            {**game, "id": True},
            {**game, "type": 1},
            {**game, "type": True},
            {**game, "name": None, "name_cn": " "},
        ]

        for fixture in invalid_subjects:
            with self.subTest(fixture=fixture):
                cache.clear()
                mock_api_request.return_value = fixture

                with self.assertRaises(ProviderAPIError) as context:
                    bangumi.subject(313495, MediaTypes.GAME.value)

                self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
                self.assertIn("Invalid Bangumi response", str(context.exception))
                self.assertIsNotNone(context.exception.__cause__)
                self.assertIsInstance(context.exception.__cause__, ValueError)

    @patch("app.providers.bangumi.cache.set")
    @patch("app.providers.bangumi.services.api_request")
    def test_invalid_detail_is_never_cached(self, mock_api_request, mock_cache_set):
        """Validation completes before a detail result enters the cache."""
        mock_api_request.return_value = load_fixture("bangumi_malformed.json")

        with self.assertRaises(ProviderAPIError):
            bangumi.subject(313495, MediaTypes.GAME.value)

        mock_cache_set.assert_not_called()

    @patch("app.providers.bangumi.services.api_request")
    def test_detail_cache_returns_identical_result_after_one_http_call(
        self,
        mock_api_request,
    ):
        """A normalized detail is cached only after it is complete."""
        mock_api_request.return_value = load_fixture("bangumi_game.json")

        first = bangumi.subject(313495, MediaTypes.GAME.value)
        second = bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertEqual(first, second)
        mock_api_request.assert_called_once()

    @patch("app.providers.bangumi.services.api_request")
    def test_same_numeric_id_for_book_and_game_has_distinct_cache_entries(
        self,
        mock_api_request,
    ):
        """Media type is part of the detail cache identity."""
        book = load_fixture("bangumi_book.json")
        game = load_fixture("bangumi_game.json")
        game["id"] = 9585
        mock_api_request.side_effect = [book, game]

        book_response = bangumi.subject(9585, MediaTypes.BOOK.value)
        game_response = bangumi.subject(9585, MediaTypes.GAME.value)

        self.assertEqual(book_response["media_type"], MediaTypes.BOOK.value)
        self.assertEqual(game_response["media_type"], MediaTypes.GAME.value)
        self.assertEqual(mock_api_request.call_count, 2)

    @patch("app.providers.bangumi.cache.set")
    @patch("app.providers.bangumi.cache.get")
    @patch("app.providers.bangumi.services.api_request")
    def test_detail_uses_exact_cache_key(
        self,
        mock_api_request,
        mock_cache_get,
        mock_cache_set,
    ):
        """Bangumi detail cache identity follows the required exact format."""
        cached = {"media_id": 313495}
        mock_cache_get.return_value = cached

        response = bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertIs(response, cached)
        mock_cache_get.assert_called_once_with("bangumi_game_313495")
        mock_cache_set.assert_not_called()
        mock_api_request.assert_not_called()

    @patch("app.providers.bangumi.services.api_request")
    def test_detail_timeout_becomes_chained_provider_error(self, mock_api_request):
        """Timeouts are normalized as chained Bangumi provider errors."""
        error = requests.exceptions.Timeout("timed out")
        mock_api_request.side_effect = error

        with self.assertRaises(ProviderAPIError) as context:
            bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
        self.assertIs(context.exception.__cause__, error)

    @patch("app.providers.bangumi.services.api_request")
    def test_detail_http_500_becomes_chained_provider_error(self, mock_api_request):
        """HTTP 500 failures are normalized as chained provider errors."""
        response = requests.Response()
        response.status_code = 500
        response._content = b"server error"
        error = requests.exceptions.HTTPError("server error", response=response)
        mock_api_request.side_effect = error

        with self.assertRaises(ProviderAPIError) as context:
            bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
        self.assertEqual(context.exception.status_code, 500)
        self.assertIs(context.exception.__cause__, error)

    @patch("app.providers.bangumi.services.api_request")
    def test_detail_json_decode_becomes_chained_provider_error(self, mock_api_request):
        """Invalid JSON failures are normalized as chained provider errors."""
        error = requests.exceptions.JSONDecodeError("invalid JSON", "{", 1)
        mock_api_request.side_effect = error

        with self.assertRaises(ProviderAPIError) as context:
            bangumi.subject(313495, MediaTypes.GAME.value)

        self.assertEqual(context.exception.provider, Sources.BANGUMI.value)
        self.assertIs(context.exception.__cause__, error)


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
