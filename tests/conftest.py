"""Sets the environment before any test module imports the service.

get_settings() caches its first result, so these values must exist before main.py is imported.
"""

import os

os.environ.setdefault("API_SECRET", "test-secret-0123456789")
os.environ.setdefault("SCHEDULER_DB_PATH", ":memory:")
