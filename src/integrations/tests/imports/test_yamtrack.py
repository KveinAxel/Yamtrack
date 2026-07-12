from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    TV,
    Anime,
    Book,
    Episode,
    Manga,
    Movie,
    Season,
)
from app.providers import services
from integrations.imports import (
    yamtrack,
)
from integrations.imports.helpers import MediaImportError

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportYamtrack(TestCase):
    """Test importing media from Yamtrack CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_yamtrack.csv").open("rb") as file:
            self.import_results = yamtrack.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported media."""
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Manga.objects.filter(user=self.user).count(), 1)
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Season.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            24,
        )

    def test_historical_records(self):
        """Test historical records creation during import."""
        anime = Anime.objects.filter(user=self.user).first()
        self.assertEqual(anime.history.count(), 1)
        self.assertEqual(
            anime.history.first().history_date,
            datetime(2024, 2, 9, 10, 0, 0, tzinfo=UTC),
        )

        movie = Movie.objects.filter(user=self.user).first()
        self.assertEqual(movie.history.count(), 1)
        self.assertEqual(
            movie.history.first().history_date,
            datetime(2024, 2, 9, 15, 30, 0, tzinfo=UTC),
        )

        tv = TV.objects.filter(user=self.user).first()
        self.assertEqual(tv.history.count(), 1)
        self.assertEqual(
            tv.history.first().history_date,
            datetime(2024, 2, 9, 12, 0, 0, tzinfo=UTC),
        )

    def test_missing_metadata_handling(self):
        """Test _handle_missing_metadata method directly."""
        test_rows = [
            # TV Show
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "tv",
                "title": "",
                "image": "",
                "season_number": "",
                "episode_number": "",
            },
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "season",
                "title": "",
                "image": "",
                "season_number": "2",
                "episode_number": "",
            },
            # Episode
            {
                "media_id": "1668",
                "source": "tmdb",
                "media_type": "episode",
                "title": "",
                "image": "",
                "season_number": "2",
                "episode_number": "5",
            },
        ]

        importer = yamtrack.YamtrackImporter(None, self.user, "new")

        for row in test_rows:
            # Make copies of original rows to verify they're modified
            original_row = row.copy()

            # Call the method directly
            importer._handle_missing_metadata(
                row,
                row["media_type"],
                row["season_number"],
                row["episode_number"],
            )

            self.assertNotEqual(row["title"], original_row["title"])
            self.assertNotEqual(row["image"], original_row["image"])


class ImportYamtrackPartials(TestCase):
    """Test importing yamtrack media with no ID."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_yamtrack_partials.csv").open("rb") as file:
            self.import_results = yamtrack.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported media."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

    def test_season_episode_search_by_title(self):
        """Test that seasons and episodes can be resolved by title (no media_id).

        This test verifies the fix that allows searching for SEASON and EPISODE
        media types by searching for the parent TV show on TMDB. Before the fix,
        services.search() didn't handle SEASON/EPISODE types and would fail with:
        UnboundLocalError: cannot access local variable 'response'
        """
        test_rows = [
            # Season with title only (no media_id)
            {
                "media_id": "",
                "source": "",
                "media_type": "season",
                "title": "Friends",
                "image": "",
                "season_number": "1",
                "episode_number": "",
            },
            # Episode with title only (no media_id)
            {
                "media_id": "",
                "source": "",
                "media_type": "episode",
                "title": "Friends",
                "image": "",
                "season_number": "1",
                "episode_number": "1",
            },
        ]

        importer = yamtrack.YamtrackImporter(None, self.user, "new")

        for row in test_rows:
            original_row = row.copy()

            importer._handle_missing_metadata(
                row,
                row["media_type"],
                int(row["season_number"]) if row["season_number"] else None,
                int(row["episode_number"]) if row["episode_number"] else None,
            )

            # Verify media_id was resolved from TMDB search
            self.assertNotEqual(row["media_id"], original_row["media_id"])
            self.assertEqual(str(row["media_id"]), "1668")  # Friends TV show ID
            self.assertEqual(row["source"], "tmdb")
            # Title and image should be populated from TMDB
            self.assertNotEqual(row["title"], "")
            self.assertNotEqual(row["image"], "")

    def test_end_dates(self):
        """Test end dates during import."""
        book = Book.objects.filter(user=self.user).first()
        self.assertEqual(book.history.count(), 1)
        bookqs = Book.objects.filter(user=self.user).order_by("-end_date")
        books = list(bookqs)

        self.assertEqual(len(books), 3)
        self.assertEqual(
            books[0].end_date,
            datetime(2024, 5, 9, 0, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            books[1].end_date,
            datetime(2024, 4, 9, 0, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(
            books[2].end_date,
            datetime(2024, 3, 9, 0, 0, 0, tzinfo=UTC),
        )


class YamtrackMissingMetadataTests(TestCase):
    """Test deterministic provider selection for title-only import rows."""

    def setUp(self):
        """Create an importer without reading a CSV file."""
        self.user = get_user_model().objects.create_user(username="fallback-test")
        self.importer = yamtrack.YamtrackImporter(None, self.user, "new")

    @patch("integrations.imports.yamtrack.services.search")
    def test_blank_source_falls_back_in_configured_order(self, mock_search):
        """Empty results advance providers sequentially and stop on a hit."""
        mock_search.side_effect = [
            {"results": []},
            {
                "results": [
                    {
                        "title": "Warlock",
                        "source": "hardcover",
                        "media_id": "book-1",
                        "image": "https://example.invalid/warlock.jpg",
                    },
                ],
            },
        ]
        row = {
            "media_id": "",
            "source": "",
            "media_type": "book",
            "title": "0312980388",
            "image": "",
        }

        self.importer._handle_missing_metadata(row, "book", None, None)

        self.assertEqual(
            mock_search.call_args_list,
            [
                call("book", "0312980388", 1, "bangumi"),
                call("book", "0312980388", 1, "hardcover"),
            ],
        )
        self.assertEqual(row["source"], "hardcover")
        self.assertEqual(row["media_id"], "book-1")
        self.assertEqual(row["title"], "Warlock")

    @patch("integrations.imports.yamtrack.services.search")
    def test_blank_source_stops_after_first_provider_hit(self, mock_search):
        """A Bangumi hit prevents calls to later configured providers."""
        mock_search.return_value = {
            "results": [
                {
                    "title": "葬送的芙莉莲",
                    "source": "bangumi",
                    "media_id": 400602,
                    "image": "https://example.invalid/frieren.jpg",
                },
            ],
        }
        row = {
            "media_id": "",
            "source": "",
            "media_type": "anime",
            "title": "葬送的芙莉莲",
            "image": "",
        }

        self.importer._handle_missing_metadata(row, "anime", None, None)

        mock_search.assert_called_once_with("anime", "葬送的芙莉莲", 1, "bangumi")
        self.assertEqual(row["source"], "bangumi")

    @patch("integrations.imports.yamtrack.services.search")
    def test_explicit_source_empty_result_does_not_fallback(self, mock_search):
        """An explicit source is authoritative even when it has no results."""
        mock_search.return_value = {"results": []}
        row = {
            "media_id": "",
            "source": "hardcover",
            "media_type": "book",
            "title": "Missing Book",
            "image": "",
        }

        with self.assertRaises(MediaImportError) as context:
            self.importer._handle_missing_metadata(row, "book", None, None)

        mock_search.assert_called_once_with("book", "Missing Book", 1, "hardcover")
        self.assertIn("book", str(context.exception))
        self.assertIn("Missing Book", str(context.exception))
        self.assertIn("hardcover", str(context.exception))

    @patch("integrations.imports.yamtrack.services.search")
    def test_all_configured_sources_empty_raises_clear_error(self, mock_search):
        """Exhausting configured sources raises MediaImportError, never IndexError."""
        mock_search.return_value = {"results": []}
        row = {
            "media_id": "",
            "source": "",
            "media_type": "book",
            "title": "Missing Book",
            "image": "",
        }

        with self.assertRaises(MediaImportError) as context:
            self.importer._handle_missing_metadata(row, "book", None, None)

        self.assertEqual(
            mock_search.call_args_list,
            [
                call("book", "Missing Book", 1, "bangumi"),
                call("book", "Missing Book", 1, "hardcover"),
                call("book", "Missing Book", 1, "openlibrary"),
            ],
        )
        self.assertIn("book", str(context.exception))
        self.assertIn("Missing Book", str(context.exception))
        self.assertIn("bangumi, hardcover, openlibrary", str(context.exception))

    @patch("integrations.imports.yamtrack.services.search")
    def test_provider_error_is_not_treated_as_empty_result(self, mock_search):
        """Provider failures propagate instead of triggering another provider."""
        error = services.ProviderAPIError(
            "bangumi",
            ConnectionError("Bangumi unavailable"),
        )
        mock_search.side_effect = error
        row = {
            "media_id": "",
            "source": "",
            "media_type": "book",
            "title": "Book",
            "image": "",
        }

        with self.assertRaises(services.ProviderAPIError) as context:
            self.importer._handle_missing_metadata(row, "book", None, None)

        self.assertIs(context.exception, error)
        mock_search.assert_called_once_with("book", "Book", 1, "bangumi")

    @patch("integrations.imports.yamtrack.services.search")
    def test_blank_season_source_uses_only_tmdb(self, mock_search):
        """Season title resolution preserves its configured TMDB provider."""
        mock_search.return_value = {
            "results": [
                {
                    "title": "Friends",
                    "source": "tmdb",
                    "media_id": 1668,
                    "image": "https://example.invalid/friends.jpg",
                },
            ],
        }
        row = {
            "media_id": "",
            "source": "",
            "media_type": "season",
            "title": "Friends",
            "image": "",
        }

        self.importer._handle_missing_metadata(row, "season", 1, None)

        mock_search.assert_called_once_with("season", "Friends", 1, "tmdb")
        self.assertEqual(row["source"], "tmdb")
        self.assertEqual(row["media_id"], 1668)

    @patch("integrations.imports.yamtrack.services.search")
    def test_import_data_preserves_clear_all_empty_error(self, mock_search):
        """The public importer does not wrap an expected no-results error."""
        mock_search.return_value = {"results": []}
        csv_file = BytesIO(
            b"media_id,source,media_type,title,image,season_number,episode_number,"
            b"progress,status\n,,book,Missing Book,,,,0,Completed\n",
        )
        importer = yamtrack.YamtrackImporter(csv_file, self.user, "new")

        with self.assertRaises(MediaImportError) as context:
            importer.import_data()

        self.assertIn("book", str(context.exception))
        self.assertIn("Missing Book", str(context.exception))
        self.assertIn("bangumi, hardcover, openlibrary", str(context.exception))
