import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS behaviour")


@pytest.fixture
def home(tmp_path):
    """A small, self-contained Hermes home (never the real one)."""
    root = tmp_path / "hermes-home"
    for folder in ("cache/uv", "logs", "audio_cache", "image_cache"):
        (root / folder).mkdir(parents=True)
    (root / "logs" / "gateway.log").write_bytes(b"x" * 200_000)
    (root / "image_cache" / "a.png").write_bytes(os.urandom(150_000))
    (root / "config.yaml").write_text("model: test\n")
    (root / ".env").write_text("API_KEY=not-a-real-key\n")
    return root
