# Homelab Chinese metadata fork

`homelab-zh` is based on reviewed Yamtrack release tags. It adds Chinese-first
metadata providers for the private KveinAxel Homelab. `dev` tracks upstream;
upstream `dev` is never auto-merged into `homelab-zh`.

## Operator smoke test

After building and starting the image, run this read-only check against the
public Bangumi API from inside the container (replace the container name if
needed):

```sh
YAMTRACK_CONTAINER=yamtrack
docker exec -i -w /yamtrack "$YAMTRACK_CONTAINER" \
  /yamtrack/.venv/bin/python manage.py shell <<'PY'
import requests

from app.models import MediaTypes
from app.providers import bangumi

cases = (
    (MediaTypes.ANIME.value, "葬送的芙莉莲", 400602, 2),
    (MediaTypes.GAME.value, "黑神话：悟空", 313495, 4),
    (MediaTypes.BOOK.value, "三体", 9585, 1),
)

for media_type, expected_title, expected_id, expected_type in cases:
    actual_type = bangumi.SUBJECT_TYPES[media_type]
    assert actual_type == expected_type, (media_type, actual_type, expected_type)
    response = requests.post(
        f"{bangumi.BASE_URL}/search/subjects",
        params={"limit": bangumi.SEARCH_PAGE_LIMIT, "offset": 0},
        json={
            "keyword": expected_title,
            "sort": "match",
            "filter": {"type": [actual_type]},
        },
        headers={"User-Agent": bangumi.USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    matches = [row for row in response.json()["data"] if row.get("id") == expected_id]
    assert matches, (expected_title, expected_id, response.json()["data"])
    subject = matches[0]
    assert subject.get("type") == expected_type, subject
    assert bangumi.localized_title(subject) == expected_title, subject
    print(expected_title, expected_id, expected_type, "OK")
PY
```

The command sends no credentials and does not mutate the Yamtrack database.
These live-API smoke tests are operator checks, not CI tests.

## Book sources (NeoDB and Douban)

Ordinary Chinese books are served by two additional first-class sources:

- `neodb` (default book source): the open `neodb.social` catalog API. Search
  uses `GET /api/catalog/search?category=book`; metadata uses
  `GET /api/book/<uuid>`. Anonymous read access only; the provider must never
  call `catalog/fetch`, which has produced mis-merged items.
- `douban` (explicit fallback button): the unofficial
  `book.douban.com/j/subject_suggest` endpoint plus subject-page parsing
  (ld+json, og meta, `#info` panel). Douban has no official API, so this
  surface is fragile by design: anti-bot interstitials and layout changes must
  raise explicit Douban provider errors, never silent fallbacks. Traffic is
  limited to 1 request/second and detail pages are Redis-cached.

Operator smoke test for both sources (read-only, no credentials):

```sh
YAMTRACK_CONTAINER=yamtrack
docker exec -i -w /yamtrack "$YAMTRACK_CONTAINER" \
  /yamtrack/.venv/bin/python manage.py shell <<'PY'
from app.providers import douban, neodb

results = neodb.search("book", "缠斗", 1)["results"]
assert any(r["media_id"] == "54lhOEeEYQP0eyJJaMdVUX" for r in results), results
book = neodb.book("54lhOEeEYQP0eyJJaMdVUX")
assert book["title"] == "缠斗", book
assert book["details"]["isbn"] == "9787512007307", book
print("neodb", book["title"], book["details"]["publish_date"], "OK")

results = douban.search("book", "缠斗", 1)["results"]
assert any(r["media_id"] == "38409776" for r in results), results
book = douban.book("38409776")
assert book["title"] == "缠斗", book
assert "翟东升" in (book["details"]["author"] or ""), book
print("douban", book["title"], book["details"]["publish_date"], "OK")
PY
```

## Rollback boundary

Before every image change, follow the Homelab Yamtrack runbook and create a
validated logical dump. After Bangumi-backed rows exist, rollback requires a
Bangumi-aware custom image or restoring the pre-custom dump and discarding
newer Bangumi rows. After NeoDB- or Douban-backed rows exist, rollback
additionally requires an image that understands both sources (the first such
image supersedes `sha-6ee2cecf` as the oldest safe target); restoring an older
image means restoring the matching pre-deployment dump and discarding newer
rows.
