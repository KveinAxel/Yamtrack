from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BangumiSourceMigrationTests(TransactionTestCase):
    """Verify the source constraint expands without rewriting existing items."""

    migrate_from = ("app", "0061_episode_item_not_null")
    migrate_to = ("app", "0062_add_bangumi_source")

    def setUp(self):
        """Create a pre-migration row, then migrate to the Bangumi constraint."""
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Item = old_apps.get_model("app", "Item")
        self.existing_item_id = Item.objects.create(
            media_id="1396",
            source="tmdb",
            media_type="tv",
            title="Breaking Bad",
            image="https://example.invalid/breaking-bad.jpg",
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        """Restore the current migration state for subsequent tests."""
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_source_constraint_accepts_bangumi_and_preserves_existing_rows(self):
        """The expanded constraint preserves old rows and rejects unknown sources."""
        Item = self.apps.get_model("app", "Item")

        existing_item = Item.objects.get(pk=self.existing_item_id)
        self.assertEqual(existing_item.media_id, "1396")
        self.assertEqual(existing_item.source, "tmdb")
        self.assertEqual(existing_item.title, "Breaking Bad")

        bangumi_item = Item.objects.create(
            media_id="400602",
            source="bangumi",
            media_type="anime",
            title="葬送的芙莉莲",
            image="https://example.invalid/frieren.jpg",
        )
        self.assertEqual(bangumi_item.source, "bangumi")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(
                media_id="invalid",
                source="unknown",
                media_type="anime",
                title="Invalid source",
                image="https://example.invalid/invalid.jpg",
            )

        self.assertTrue(Item.objects.filter(pk=self.existing_item_id).exists())
