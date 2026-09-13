from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jarvis.models.jarvis_types import SpiralTurn
from jarvis.persistence import JarvisStore


def test_turn_roundtrip_and_immutable_duplicate(tmp_path: Path) -> None:
    store = JarvisStore(tmp_path / "jarvis.sqlite3")
    turn = SpiralTurn(
        turn_id="t1",
        session_id="s1",
        timestamp=datetime.now(timezone.utc),
        decision="answer",
        uncertainty=0.1,
        stress=0,
        content="ok",
        content_sha256=store.content_hash("ok"),
    )
    store.save_turn(turn)
    assert store.load_session_turns("s1")[0]["turn_id"] == "t1"
    try:
        store.save_turn(turn)
    except Exception:
        pass
    else:
        raise AssertionError("duplicate turn must not replace immutable history")
