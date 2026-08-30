"""Point the recommender at a throwaway DB before app.main is imported."""
import os
import tempfile

os.environ.setdefault("XFETCH_DB", tempfile.mktemp(suffix="-xfetch-test.db"))
