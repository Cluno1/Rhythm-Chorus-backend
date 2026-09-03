from pathlib import Path

from rhythm_metadata_api.domain.commands import OverrideCommand, OverrideField, TrackIdentity
from rhythm_metadata_api.repositories.sqlite import SqliteTrackRepository


def test_data_survives_repository_reopen(tmp_path: Path) -> None:
    database = tmp_path / "rhythm.sqlite3"
    first = SqliteTrackRepository(str(database))
    track = first.match(TrackIdentity(audio_sha256="e" * 64))
    first.apply_overrides(
        track.id,
        track.revision,
        OverrideCommand(fields={"title": OverrideField(value="New title", content_hash="v1")}),
    )
    first.close()

    second = SqliteTrackRepository(str(database))
    loaded = second.get(track.id)
    assert loaded is not None
    assert loaded.revision == 2
    assert loaded.candidates["title"][0]["value"] == "New title"
    assert [event["operation"] for event in second.history(track.id)] == [
        "metadata.overridden",
        "track.created",
    ]
    second.close()
