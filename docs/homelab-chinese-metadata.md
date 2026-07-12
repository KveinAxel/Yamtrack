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

Before every image change, follow the Homelab Yamtrack runbook and create a
validated logical dump. After Bangumi-backed rows exist, rollback requires a
Bangumi-aware custom image or restoring the pre-custom dump and discarding
newer Bangumi rows.
