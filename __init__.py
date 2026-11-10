"""Storage Utility unified Hermes plugin."""
from pathlib import Path
import sys

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def register(ctx):
    """Retain only the real profile-qualified host task context."""
    from storage_utility.runtime import register_context
    register_context(ctx)
