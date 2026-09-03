import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rhythm_metadata_api.domain.commands import (
    OverrideCommand,
    OverrideField,
    TrackIdentity,
)
from rhythm_metadata_api.repositories.memory import (
    InMemoryTrackRepository,
    RevisionConflict,
)


class RepositoryTest(unittest.TestCase):
    def test_strong_hash_matches_existing_track(self) -> None:
        repository = InMemoryTrackRepository()
        request = TrackIdentity(audio_sha256="a" * 64)
        first = repository.match(request)
        second = repository.match(request)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.matched_by, "audio_sha256")

    def test_stale_override_revision_is_rejected(self) -> None:
        repository = InMemoryTrackRepository()
        track = repository.match(TrackIdentity(audio_sha256="b" * 64))
        patch = OverrideCommand(
            fields={"lyrics": OverrideField(value="hello", content_hash="lyrics-v1")}
        )
        repository.apply_overrides(track.id, track.revision, patch)
        with self.assertRaises(RevisionConflict):
            repository.apply_overrides(track.id, track.revision, patch)


if __name__ == "__main__":
    unittest.main()
