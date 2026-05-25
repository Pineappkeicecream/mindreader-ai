"""Smoke checks for MindReader AI.

Run with a Python environment that has the project requirements installed:
python tests/smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import server


def main() -> None:
    client = TestClient(server.app)

    home = client.get("/")
    assert home.status_code == 200
    assert "MindReader AI" in home.text

    stats = client.get("/api/stats")
    assert stats.status_code == 200
    assert stats.json()["domains"] >= 6

    sessions = client.get("/api/sessions?limit=5")
    assert sessions.status_code == 200
    assert isinstance(sessions.json(), list)

    formatted = client.post(
        "/api/format",
        json={
            "format": "midjourney",
            "prompt": "## Scene\nvertical logo poster\n\n## Negative Prompt\nno blur, no watermark",
        },
    )
    assert formatted.status_code == 200
    assert "--ar" in formatted.json()["formatted"]

    print("Smoke checks passed")


if __name__ == "__main__":
    main()
