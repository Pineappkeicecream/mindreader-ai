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
import db


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

    unique_summary = "Smoke private gallery check"
    db.save_session("smoke-gallery-session", "general", "hybrid", "smoke gallery", user_id="smoke-owner")
    prompt_id = db.save_prompt(
        "smoke-gallery-session",
        "## Scene\n" + ("private gallery test prompt " * 40),
        unique_summary,
        "general",
        "smoke gallery",
    )
    gallery = client.get("/api/gallery?limit=50")
    assert gallery.status_code == 200
    assert all(p["id"] != prompt_id for p in gallery.json())

    private_lookup = client.get(f"/api/prompts/{prompt_id}")
    assert private_lookup.json().get("error") == "Prompt not found"
    owner_lookup = client.get(f"/api/prompts/{prompt_id}?user_id=smoke-owner")
    assert owner_lookup.status_code == 200
    assert owner_lookup.json()["id"] == prompt_id

    wrong_publish = client.post(
        f"/api/prompts/{prompt_id}/publish",
        json={"is_public": True, "user_id": "not-owner"},
    )
    assert wrong_publish.status_code == 200
    assert wrong_publish.json()["ok"] is False

    publish = client.post(
        f"/api/prompts/{prompt_id}/publish",
        json={"is_public": True, "user_id": "smoke-owner"},
    )
    assert publish.status_code == 200
    assert publish.json()["ok"] is True
    gallery = client.get("/api/gallery?limit=50")
    assert any(p["id"] == prompt_id for p in gallery.json())

    share = client.post(f"/api/prompts/{prompt_id}/share", json={"user_id": "smoke-owner"})
    assert share.status_code == 200
    assert share.json()["ok"] is True
    shared_page = client.get(share.json()["url"])
    assert shared_page.status_code == 200
    db.delete_prompt(prompt_id, user_id="smoke-owner")

    db.save_session("smoke-user-a", "general", "hybrid", "user a", user_id="smoke-a")
    db.save_session("smoke-user-b", "general", "hybrid", "user b", user_id="smoke-b")
    prompt_a = db.save_prompt("smoke-user-a", "## A\n" + ("a " * 300), "user a private", "general", "a")
    prompt_b = db.save_prompt("smoke-user-b", "## B\n" + ("b " * 300), "user b private", "general", "b")
    mine = client.get("/api/prompts?limit=50&user_id=smoke-a")
    ids = {p["id"] for p in mine.json()}
    assert prompt_a in ids
    assert prompt_b not in ids
    anonymous_prompts = client.get("/api/prompts?limit=50")
    assert anonymous_prompts.json() == []
    wrong_session = client.get("/api/session/smoke-user-b?user_id=smoke-a")
    assert wrong_session.json().get("error") == "Session not found"
    db.delete_prompt(prompt_a, user_id="smoke-a")
    db.delete_prompt(prompt_b, user_id="smoke-b")

    print("Smoke checks passed")


if __name__ == "__main__":
    main()
