"""CLI compatibility facade for the canonical geometry-cleaning engine.

The implementation lives under ``backend.engines`` so production imports do
not depend on the scripts directory. Existing operators and tests can keep
the historical import path while migrating to the engine package.
"""

from backend.engines.geometry_first_clean import *  # noqa: F401,F403


if __name__ == "__main__":
    from backend.engines.geometry_first_clean import main

    raise SystemExit(main())
