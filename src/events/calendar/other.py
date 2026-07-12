import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app import config
from app.models import MediaTypes, Sources
from app.providers import services
from events.models import Event

from .helpers import date_parser

logger = logging.getLogger(__name__)


def _has_bangumi_anime_progress(item, content_number):
    """Return whether a Bangumi anime has usable aggregate progress."""
    return (
        item.source == Sources.BANGUMI.value
        and item.media_type == MediaTypes.ANIME.value
        and not isinstance(content_number, bool)
        and isinstance(content_number, int)
        and content_number > 0
    )


def process_other(item, events_bulk):
    """Process other types of items and add events to the event list."""
    logger.info("Fetching releases for %s", item)
    try:
        metadata = services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
        )
    except services.ProviderAPIError:
        logger.warning(
            "Failed to fetch metadata for %s",
            item,
        )
        return

    date_key = config.get_date_key(item.media_type)
    content_number = metadata["max_progress"]

    if date_key in metadata["details"] and content_number:
        if metadata["details"][date_key]:
            try:
                content_datetime = date_parser(metadata["details"][date_key])
            except ValueError:
                logger.warning(
                    "Invalid %s date for %s: %s",
                    date_key,
                    item,
                    metadata["details"][date_key],
                )
                return
        else:
            content_datetime = datetime.min.replace(tzinfo=ZoneInfo("UTC"))

        if item.media_type == MediaTypes.MOVIE.value:
            content_number = None

        events_bulk.append(
            Event(
                item=item,
                content_number=content_number,
                datetime=content_datetime,
            ),
        )

    elif (
        item.media_type == MediaTypes.GAME.value
        and date_key in metadata["details"]
        and metadata["details"][date_key]
    ):
        try:
            content_datetime = date_parser(metadata["details"][date_key])
        except ValueError:
            logger.warning(
                "Invalid %s date for %s: %s",
                date_key,
                item,
                metadata["details"][date_key],
            )
            return

        events_bulk.append(
            Event(
                item=item,
                content_number=None,
                datetime=content_datetime,
            ),
        )

    elif (
        item.source == Sources.MANGAUPDATES.value and content_number
    ) or _has_bangumi_anime_progress(item, content_number):
        content_datetime = datetime.min.replace(tzinfo=ZoneInfo("UTC"))
        events_bulk.append(
            Event(
                item=item,
                content_number=content_number,
                datetime=content_datetime,
            ),
        )
