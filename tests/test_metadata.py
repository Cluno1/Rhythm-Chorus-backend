import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rhythm_metadata_api.domain.metadata import (
    MetadataCandidate,
    ResolutionPolicy,
    SourceType,
    resolve_field,
)


def candidate(
    value: str,
    source: SourceType,
    *,
    edited: bool = False,
    pinned: bool = False,
) -> MetadataCandidate:
    return MetadataCandidate(
        value=value,
        source=source,
        content_hash=value,
        user_edited=edited,
        pinned=pinned,
    )


class MetadataResolutionTest(unittest.TestCase):
    def test_local_first_prefers_sidecar_over_cloud(self) -> None:
        result = resolve_field(
            [
                candidate("cloud", SourceType.RHYTHM_CLOUD),
                candidate("lrc", SourceType.LOCAL_SIDECAR),
            ],
            ResolutionPolicy.LOCAL_FIRST,
        )
        self.assertEqual(result.selected.value, "lrc")

    def test_cloud_first_prefers_private_cloud(self) -> None:
        result = resolve_field(
            [
                candidate("embedded", SourceType.EMBEDDED_FILE),
                candidate("cloud", SourceType.RHYTHM_CLOUD),
            ],
            ResolutionPolicy.CLOUD_FIRST,
        )
        self.assertEqual(result.selected.value, "cloud")

    def test_unique_pin_wins(self) -> None:
        result = resolve_field(
            [
                candidate("local", SourceType.LOCAL_SIDECAR),
                candidate("chosen", SourceType.RHYTHM_CLOUD, pinned=True),
            ]
        )
        self.assertEqual(result.selected.value, "chosen")

    def test_two_different_user_edits_conflict(self) -> None:
        result = resolve_field(
            [
                candidate("phone edit", SourceType.LOCAL_SIDECAR, edited=True),
                candidate("cloud edit", SourceType.RHYTHM_CLOUD, edited=True),
            ]
        )
        self.assertTrue(result.conflict)
        self.assertIsNone(result.selected)

    def test_ask_policy_conflicts_on_difference(self) -> None:
        result = resolve_field(
            [
                candidate("embedded", SourceType.EMBEDDED_FILE),
                candidate("api", SourceType.THIRD_PARTY_API),
            ],
            ResolutionPolicy.ASK_ON_DIFFERENCE,
        )
        self.assertTrue(result.conflict)


if __name__ == "__main__":
    unittest.main()
