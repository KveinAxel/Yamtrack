# Homelab Chinese metadata fork

`homelab-zh` is based on reviewed Yamtrack release tags. It adds Chinese-first
metadata providers for the private KveinAxel Homelab. `dev` tracks upstream;
upstream `dev` is never auto-merged into `homelab-zh`.

Before every image change, follow the Homelab Yamtrack runbook and create a
validated logical dump. After Bangumi-backed rows exist, rollback requires a
Bangumi-aware custom image or restoring the pre-custom dump and discarding
newer Bangumi rows.
