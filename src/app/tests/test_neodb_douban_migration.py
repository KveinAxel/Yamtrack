from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class NeodbDoubanSourceMigrationTests(TransactionTestCase):
    """Verify the source constraint expands without rewriting existing items."""

    migrate_from = ("app", "0062_add_bangumi_source")
    migrate_to = ("app", "0063_add_neodb_douban_sources")

    def setUp(self):
        """Create pre-migration rows, then migrate to the expanded constraint."""
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        Item = old_apps.get_model("app", "Item")
        self.existing_item_id = Item.objects.create(
            media_id="509252",
            source="bangumi",
            media_type="book",
            title="三体",
            image="https://example.invalid/three-body.jpg",
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        """Restore the current migration state for subsequent tests."""
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_constraint_accepts_new_sources_and_preserves_existing_rows(self):
        """The expanded constraint preserves old rows and accepts both sources."""
        Item = self.apps.get_model("app", "Item")

        existing_item = Item.objects.get(pk=self.existing_item_id)
        self.assertEqual(existing_item.media_id, "509252")
        self.assertEqual(existing_item.source, "bangumi")
        self.assertEqual(existing_item.title, "三体")

        neodb_item = Item.objects.create(
            media_id="54lhOEeEYQP0eyJJaMdVUX",
            source="neodb",
            media_type="book",
            title="缠斗",
            image="https://example.invalid/chandou-2013.jpg",
        )
        self.assertEqual(neodb_item.source, "neodb")

        douban_item = Item.objects.create(
            media_id="38409776",
            source="douban",
            media_type="book",
            title="缠斗",
            image="https://example.invalid/chandou-2026.jpg",
        )
        self.assertEqual(douban_item.source, "douban")

    def test_constraint_still_rejects_unknown_sources(self):
        """Unknown source strings remain rejected after the migration."""
        Item = self.apps.get_model("app", "Item")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(
                media_id="invalid",
                source="weread",
                media_type="book",
                title="Invalid source",
                image="https://example.invalid/invalid.jpg",
            )
