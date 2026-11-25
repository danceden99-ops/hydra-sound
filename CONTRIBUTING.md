# Contributing to Hydra Sound

## Updating or Adding a Streaming Renderer Module

Hydra Sound isolates each streaming protocol so upstream changes can be updated independently without touching the core orchestrator or audio pipeline:

1. **Adapter:** Add or edit the protocol adapter in `backend/app/renderers/<protocol>.py` inheriting from `RendererAdapter` (`backend/app/renderers/base.py`).
2. **Update Script:** Add or update `scripts/modules/update_<protocol>.sh` so the Web GUI's module updater can pull/build the latest upstream release.
3. **Event Hook:** Add an event hook script in `scripts/hooks/<protocol>_event.sh` that POSTs `play`, `pause`, `stop`, and stream format metadata to `http://127.0.0.1:8000/api/internal/event`.
4. **Tests:** Add unit tests in `backend/tests/` and verify `pytest backend/tests -v` passes.
