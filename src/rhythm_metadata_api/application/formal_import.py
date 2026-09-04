from __future__ import annotations

import csv
import json
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    AssetSource,
    ChangeEvent,
    ChangeEventWork,
    Contributor,
    Release,
    ReleaseItem,
    Rendition,
    RenditionAsset,
    Score,
    ScoreRevision,
    ScoreRevisionAsset,
    Work,
    WorkAlias,
    WorkCredit,
)

MUSICXML_MEDIA_TYPE = "application/vnd.recordare.musicxml+xml"
IMPORT_NAMESPACE = uuid.UUID("8d63da94-1235-4a99-8807-c84b77168060")
MONGO_ID_PATTERN = re.compile(r"^gmusic:([^:]+):rev(\d+)$")


class FormalImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScoreManifestRow:
    mongo_id: str
    work_key: str
    revision_no: int
    title: str
    status: str
    cos_bucket: str
    cos_key: str
    sha256: str
    byte_size: int
    media_type: str = MUSICXML_MEDIA_TYPE
    language: str | None = None
    lyrics: str | None = None
    composers: tuple[str, ...] = ()
    lyricists: tuple[str, ...] = ()
    key_signature: str | None = None
    time_signature: str | None = None
    tempo: str | None = None
    year: str | None = None
    verified: bool = False


@dataclass(frozen=True)
class SongMappingRow:
    album_key: str
    mp3_cos_key: str
    source_filename: str
    song_title: str
    language_variant: str | None
    track_no_hint: int | None
    mongo_work_key: str | None
    score_status: str
    match_method: str
    confidence: str
    evidence: str
    mp3_sha256: str
    byte_size: int
    duration_ms: int | None
    cos_bucket: str
    verified: bool = False


@dataclass(frozen=True)
class ImportSummary:
    works: int
    scores: int
    score_revisions: int
    songs: int
    albums: int
    release_items: int
    assets: int
    cos_locations: int
    contributors: int
    work_credits: int


def deterministic_id(kind: str, source_key: str) -> str:
    return str(uuid.uuid5(IMPORT_NAMESPACE, f"{kind}:{source_key}"))


def read_score_manifest(path: Path) -> list[ScoreManifestRow]:
    rows: list[ScoreManifestRow] = []
    with path.open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                mongo_id = str(value.get("mongo_id") or value["_id"])
                match = MONGO_ID_PATTERN.fullmatch(mongo_id)
                if match is None:
                    raise ValueError("invalid GMUSIC revision id")
                rows.append(
                    ScoreManifestRow(
                        mongo_id=mongo_id,
                        work_key=str(value.get("work_key") or f"gmusic:{match.group(1)}"),
                        revision_no=int(value.get("revision_no") or match.group(2)),
                        title=str(value["title"]).strip(),
                        status=str(value["status"]),
                        cos_bucket=str(value["cos_bucket"]),
                        cos_key=str(value["cos_key"]),
                        sha256=_sha256(value["sha256"]),
                        byte_size=int(value.get("byte_size") or value["bytes"]),
                        media_type=str(value.get("media_type") or MUSICXML_MEDIA_TYPE),
                        language=_optional(value.get("language") or value.get("lyrics_lang")),
                        lyrics=_optional(value.get("lyrics")),
                        composers=_names(value.get("composers") or value.get("composer")),
                        lyricists=_names(value.get("lyricists") or value.get("lyricist")),
                        key_signature=_optional(value.get("key_signature")),
                        time_signature=_optional(value.get("time_signature")),
                        tempo=_optional(value.get("tempo")),
                        year=_optional(value.get("year")),
                        verified=_bool(value.get("verified")),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise FormalImportError(f"invalid score manifest line {line_no}: {error}") from error
    return rows


def read_song_mapping(path: Path, default_bucket: str = "bible-1328751369") -> list[SongMappingRow]:
    rows: list[SongMappingRow] = []
    with path.open(encoding="utf-8", newline="") as source:
        for line_no, value in enumerate(csv.DictReader(source), 2):
            try:
                rows.append(
                    SongMappingRow(
                        album_key=str(value["album_key"]).strip(),
                        mp3_cos_key=str(value["mp3_cos_key"]).strip(),
                        source_filename=str(value.get("source_filename") or "").strip(),
                        song_title=str(value["song_title"]).strip(),
                        language_variant=_optional(value.get("language_variant")),
                        track_no_hint=_optional_int(value.get("track_no_hint")),
                        mongo_work_key=_optional(value.get("mongo_work_key")),
                        score_status=str(value.get("score_status") or "no-score").strip(),
                        match_method=str(value.get("match_method") or "song-only").strip(),
                        confidence=str(value.get("confidence") or "medium").strip(),
                        evidence=str(value.get("evidence") or "").strip(),
                        mp3_sha256=_sha256(value["mp3_sha256"]),
                        byte_size=int(value.get("byte_size") or value["bytes"]),
                        duration_ms=_optional_int(value.get("duration_ms")),
                        cos_bucket=str(value.get("cos_bucket") or default_bucket).strip(),
                        verified=_bool(value.get("verified")),
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise FormalImportError(f"invalid song mapping line {line_no}: {error}") from error
    return rows


class FormalCatalogImporter:
    """Idempotently materialize verified manifests without copying any COS object."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def validate(
        self, score_rows: list[ScoreManifestRow], song_rows: list[SongMappingRow]
    ) -> ImportSummary:
        if not score_rows:
            raise FormalImportError("score manifest is empty")
        if not song_rows:
            raise FormalImportError("song mapping is empty")
        if any(row.status not in {"canonical", "superseded", "candidate"} for row in score_rows):
            raise FormalImportError("score manifest contains an unsupported status")
        if any(not row.verified for row in score_rows):
            raise FormalImportError("every score object must be hash-verified before import")
        if any(not row.verified for row in song_rows):
            raise FormalImportError("every MP3 object must be hash-verified before import")
        if any(row.byte_size <= 0 or not row.duration_ms or row.duration_ms <= 0 for row in song_rows):
            raise FormalImportError("every MP3 must have a positive size and duration")
        if any(row.album_key != "ihope" for row in song_rows):
            raise FormalImportError("all issue 12 songs must use album_key=ihope")
        if len({row.mp3_cos_key for row in song_rows}) != len(song_rows):
            raise FormalImportError("song mapping contains duplicate COS keys")
        canonical_by_work: dict[str, int] = defaultdict(int)
        imported_work_keys: set[str] = set()
        revisions: set[tuple[str, int]] = set()
        for row in score_rows:
            if row.status == "canonical":
                canonical_by_work[row.work_key] += 1
            if row.status in {"canonical", "superseded"}:
                imported_work_keys.add(row.work_key)
            revision_key = (row.work_key, row.revision_no)
            if revision_key in revisions:
                raise FormalImportError(f"duplicate score revision {revision_key}")
            revisions.add(revision_key)
        invalid = [key for key in imported_work_keys if canonical_by_work[key] != 1]
        if invalid:
            raise FormalImportError(f"works must have one canonical revision: {invalid[:5]}")
        canonical_keys = {row.work_key for row in score_rows if row.status == "canonical"}
        candidate_only_keys = {
            row.work_key for row in score_rows if row.status == "candidate"
        } - canonical_keys
        allowed_match_methods = {
            "normalized-exact",
            "semantic-confirmed",
            "candidate-title",
            "song-only",
        }
        for row in song_rows:
            if row.match_method not in allowed_match_methods:
                raise FormalImportError(f"unsupported match method: {row.match_method}")
            if row.confidence.casefold() == "low":
                raise FormalImportError(f"low-confidence mapping is not importable: {row.mp3_cos_key}")
            if row.score_status == "canonical":
                if row.mongo_work_key not in canonical_keys or row.match_method not in {
                    "normalized-exact",
                    "semantic-confirmed",
                }:
                    raise FormalImportError(f"invalid canonical mapping: {row.mp3_cos_key}")
            elif row.score_status == "candidate-only":
                if (
                    row.mongo_work_key not in candidate_only_keys
                    or row.match_method != "candidate-title"
                ):
                    raise FormalImportError(f"invalid candidate mapping: {row.mp3_cos_key}")
            elif row.score_status == "no-score":
                if row.mongo_work_key is not None or row.match_method != "song-only":
                    raise FormalImportError(f"invalid song-only mapping: {row.mp3_cos_key}")
            else:
                raise FormalImportError(f"unsupported score status: {row.score_status}")
        imported_scores = [row for row in score_rows if row.status in {"canonical", "superseded"}]
        work_keys = {row.work_key for row in imported_scores}
        song_only = {
            self._song_work_key(row)
            for row in song_rows
            if not row.mongo_work_key or row.mongo_work_key not in work_keys
        }
        asset_keys = {row.sha256 for row in imported_scores} | {
            row.mp3_sha256 for row in song_rows
        }
        return ImportSummary(
            works=len(work_keys | song_only),
            scores=len(work_keys),
            score_revisions=len(imported_scores),
            songs=len(song_rows),
            albums=1,
            release_items=len(song_rows),
            assets=len(asset_keys),
            cos_locations=len(asset_keys),
            contributors=len(
                {
                    _normalized_name(name)
                    for row in imported_scores
                    if row.status == "canonical"
                    for name in (*row.composers, *row.lyricists)
                }
            ),
            work_credits=sum(
                len(set(row.composers)) + len(set(row.lyricists))
                for row in imported_scores
                if row.status == "canonical"
            ),
        )

    def import_all(
        self, score_rows: list[ScoreManifestRow], song_rows: list[SongMappingRow]
    ) -> ImportSummary:
        expected = self.validate(score_rows, song_rows)
        imported_scores = [row for row in score_rows if row.status in {"canonical", "superseded"}]
        by_work: dict[str, list[ScoreManifestRow]] = defaultdict(list)
        for row in imported_scores:
            by_work[row.work_key].append(row)
        arrangements: dict[str, Arrangement] = {}
        for work_key, rows in sorted(by_work.items()):
            canonical = next(row for row in rows if row.status == "canonical")
            work = self._ensure_work(work_key, canonical.title, canonical.language, canonical.lyrics)
            self._ensure_work_credits(work, canonical.composers, canonical.lyricists)
            arrangement = self._ensure_arrangement(work, work_key, canonical.key_signature)
            arrangements[work_key] = arrangement
            self._ensure_score(work_key, arrangement, rows)

        release = self._ensure_release("ihope", "ihope")
        release_work_ids: set[str] = set()
        ordered_songs = sorted(
            song_rows,
            key=lambda row: (
                row.track_no_hint is None,
                row.track_no_hint or 0,
                row.mp3_cos_key,
            ),
        )
        for display_order, row in enumerate(ordered_songs, 1):
            work_key = self._song_work_key(row)
            arrangement = arrangements.get(work_key)
            if arrangement is None:
                work = self._ensure_work(
                    work_key, row.song_title, _normalize_language(row.language_variant), None
                )
                arrangement = self._ensure_arrangement(work, work_key, None)
                arrangements[work_key] = arrangement
            release_work_ids.add(arrangement.work_id)
            asset = self._ensure_asset(
                sha256=row.mp3_sha256,
                byte_size=row.byte_size,
                media_type="audio/mpeg",
                bucket=row.cos_bucket,
                key=row.mp3_cos_key,
                original_filename=row.source_filename,
                source_ref=f"cos://{row.cos_bucket}/{row.mp3_cos_key}",
            )
            rendition = self._ensure_rendition(arrangement, row)
            self._ensure_rendition_asset(rendition, asset)
            self._ensure_release_item(release, rendition, row.track_no_hint, display_order)
        self._ensure_change_event(
            entity_type="release",
            entity_id=release.id,
            entity_revision=release.revision,
            operation="release.imported",
            work_ids=sorted(release_work_ids),
            payload={"key": release.key, "song_count": len(song_rows)},
        )
        self.session.flush()
        actual = self._summary()
        if actual != expected:
            raise FormalImportError(f"post-import counts differ: expected={expected}, actual={actual}")
        return actual

    def _ensure_work(
        self, work_key: str, title: str, language: str | None, lyrics: str | None
    ) -> Work:
        external_id = work_key.removeprefix("gmusic:")
        namespace = "gmusic" if work_key.startswith("gmusic:") else "bible_cos"
        alias = self.session.scalar(
            select(WorkAlias).where(
                WorkAlias.namespace == namespace, WorkAlias.external_id == external_id
            )
        )
        if alias is not None:
            work = self.session.get(Work, alias.work_id)
            if work is None:
                raise FormalImportError(f"dangling work alias {work_key}")
            expected = (title, language, lyrics, "active")
            actual = (work.canonical_title, work.language, work.lyrics, work.status)
            if actual != expected:
                raise FormalImportError(f"work manifest drift for {work_key}")
            return work
        work = Work(
            id=deterministic_id("work", work_key),
            canonical_title=title,
            language=language,
            lyrics=lyrics,
            status="active",
        )
        self.session.add(work)
        self.session.flush()
        self.session.add(
            WorkAlias(
                id=deterministic_id("work-alias", work_key),
                work_id=work.id,
                namespace=namespace,
                external_id=external_id,
            )
        )
        self._ensure_change_event(
            entity_type="work",
            entity_id=work.id,
            entity_revision=work.revision,
            operation="work.imported",
            work_ids=[work.id],
            payload={"source_key": work_key},
        )
        return work

    def _ensure_arrangement(
        self, work: Work, work_key: str, key_signature: str | None
    ) -> Arrangement:
        arrangement_id = deterministic_id("arrangement", work_key)
        arrangement = self.session.get(Arrangement, arrangement_id)
        if arrangement is None:
            arrangement = Arrangement(
                id=arrangement_id,
                work_id=work.id,
                name="正式编配",
                key_signature=key_signature,
            )
            self.session.add(arrangement)
            self.session.flush()
        elif (
            arrangement.work_id != work.id
            or arrangement.name != "正式编配"
            or arrangement.key_signature != key_signature
        ):
            raise FormalImportError(f"arrangement manifest drift for {work_key}")
        self._ensure_change_event(
            entity_type="arrangement",
            entity_id=arrangement.id,
            entity_revision=arrangement.revision,
            operation="arrangement.imported",
            work_ids=[work.id],
        )
        return arrangement

    def _ensure_score(
        self, work_key: str, arrangement: Arrangement, rows: list[ScoreManifestRow]
    ) -> Score:
        score_id = deterministic_id("score", work_key)
        canonical = next(row for row in rows if row.status == "canonical")
        score = self.session.get(Score, score_id)
        if score is None:
            score = Score(
                id=score_id,
                arrangement_id=arrangement.id,
                label="GMUSIC OCR",
                origin="ocr",
                lyrics=canonical.lyrics,
            )
            self.session.add(score)
            self.session.flush()
        elif (
            score.arrangement_id != arrangement.id
            or score.label != "GMUSIC OCR"
            or score.origin != "ocr"
            or score.lyrics != canonical.lyrics
        ):
            raise FormalImportError(f"score manifest drift for {work_key}")
        revisions: dict[int, ScoreRevision] = {}
        previous: ScoreRevision | None = None
        for row in sorted(rows, key=lambda value: value.revision_no):
            asset = self._ensure_asset(
                sha256=row.sha256,
                byte_size=row.byte_size,
                media_type=row.media_type,
                bucket=row.cos_bucket,
                key=row.cos_key,
                original_filename=Path(row.cos_key).name,
                source_ref=row.mongo_id,
            )
            revision_id = deterministic_id("score-revision", row.mongo_id)
            revision = self.session.get(ScoreRevision, revision_id)
            if revision is None:
                revision = ScoreRevision(
                    id=revision_id,
                    score_id=score.id,
                    revision_no=row.revision_no,
                    based_on_revision_id=previous.id if previous else None,
                    edit_message=f"Mongo {row.status}: {row.mongo_id}",
                )
                self.session.add(revision)
                self.session.flush()
            link = self.session.scalar(
                select(ScoreRevisionAsset).where(
                    ScoreRevisionAsset.score_revision_id == revision.id,
                    ScoreRevisionAsset.role == "primary_musicxml",
                )
            )
            if link is None:
                self.session.add(
                    ScoreRevisionAsset(
                        id=deterministic_id("score-revision-asset", row.mongo_id),
                        score_revision_id=revision.id,
                        asset_id=asset.id,
                        role="primary_musicxml",
                    )
                )
            elif link.asset_id != asset.id:
                raise FormalImportError(f"revision asset conflict for {row.mongo_id}")
            expected_parent = previous.id if previous else None
            if revision.based_on_revision_id != expected_parent:
                raise FormalImportError(f"revision chain conflict for {row.mongo_id}")
            revisions[row.revision_no] = revision
            previous = revision
            self._ensure_change_event(
                entity_type="score_revision",
                entity_id=revision.id,
                entity_revision=revision.revision_no,
                operation="score_revision.imported",
                work_ids=[arrangement.work_id],
                payload={"mongo_id": row.mongo_id, "status": row.status},
            )
        score.head_revision_id = revisions[max(revisions)].id
        score.published_revision_id = revisions[canonical.revision_no].id
        arrangement.preferred_score_id = score.id
        self._ensure_change_event(
            entity_type="score",
            entity_id=score.id,
            entity_revision=score.revision,
            operation="score.imported",
            work_ids=[arrangement.work_id],
        )
        return score

    def _ensure_asset(
        self,
        *,
        sha256: str,
        byte_size: int,
        media_type: str,
        bucket: str,
        key: str,
        original_filename: str,
        source_ref: str,
    ) -> Asset:
        asset = self.session.scalar(select(Asset).where(Asset.sha256 == sha256))
        if asset is None:
            asset = Asset(
                id=deterministic_id("asset", sha256),
                sha256=sha256,
                byte_size=byte_size,
                detected_media_type=media_type,
                state="ready",
            )
            self.session.add(asset)
            self.session.flush()
        elif asset.byte_size != byte_size or asset.detected_media_type != media_type:
            raise FormalImportError(f"asset metadata conflict for sha256={sha256}")
        storage_key = f"{bucket}/{key.lstrip('/')}"
        existing_location = self.session.scalar(
            select(AssetLocation).where(
                AssetLocation.provider == "cos", AssetLocation.storage_key == storage_key
            )
        )
        if existing_location is None:
            same_provider = self.session.scalar(
                select(AssetLocation).where(
                    AssetLocation.asset_id == asset.id, AssetLocation.provider == "cos"
                )
            )
            if same_provider is None:
                self.session.add(
                    AssetLocation(
                        id=deterministic_id("asset-location", storage_key),
                        asset_id=asset.id,
                        provider="cos",
                        storage_key=storage_key,
                        state="available",
                    )
                )
            elif same_provider.storage_key != storage_key:
                # The schema permits one location per provider; retain the extra key as provenance.
                pass
        elif existing_location.asset_id != asset.id:
            raise FormalImportError(f"COS location points to different content: {storage_key}")
        source = self.session.scalar(
            select(AssetSource).where(
                AssetSource.asset_id == asset.id,
                AssetSource.source == "formal_import",
                AssetSource.source_ref == source_ref,
            )
        )
        if source is None:
            self.session.add(
                AssetSource(
                    id=deterministic_id("asset-source", f"{asset.id}:{source_ref}"),
                    asset_id=asset.id,
                    original_filename=original_filename,
                    source="formal_import",
                    source_ref=source_ref,
                )
            )
        return asset

    def _ensure_rendition(self, arrangement: Arrangement, row: SongMappingRow) -> Rendition:
        rendition_id = deterministic_id("rendition", row.mp3_cos_key)
        label = row.song_title
        if _normalize_language(row.language_variant) == "en":
            label = f"{label} ({row.language_variant})"
        rendition = self.session.get(Rendition, rendition_id)
        if rendition is None:
            rendition = Rendition(
                id=rendition_id,
                arrangement_id=arrangement.id,
                label=label,
                kind="performance",
                duration_ms=row.duration_ms,
            )
            self.session.add(rendition)
            self.session.flush()
        elif (
            rendition.arrangement_id != arrangement.id
            or rendition.label != label
            or rendition.kind != "performance"
            or rendition.duration_ms != row.duration_ms
        ):
            raise FormalImportError(f"rendition manifest drift for {row.mp3_cos_key}")
        self._ensure_change_event(
            entity_type="rendition",
            entity_id=rendition.id,
            entity_revision=rendition.revision,
            operation="rendition.imported",
            work_ids=[arrangement.work_id],
            payload={"cos_key": row.mp3_cos_key},
        )
        return rendition

    def _ensure_rendition_asset(self, rendition: Rendition, asset: Asset) -> None:
        link = self.session.scalar(
            select(RenditionAsset).where(
                RenditionAsset.rendition_id == rendition.id,
                RenditionAsset.role == "stream",
            )
        )
        if link is None:
            self.session.add(
                RenditionAsset(
                    id=deterministic_id("rendition-asset", rendition.id),
                    rendition_id=rendition.id,
                    asset_id=asset.id,
                    role="stream",
                )
            )
        elif link.asset_id != asset.id:
            raise FormalImportError(f"rendition asset conflict for {rendition.id}")

    def _ensure_release(self, key: str, title: str) -> Release:
        release = self.session.scalar(select(Release).where(Release.key == key))
        if release is None:
            release = Release(id=deterministic_id("release", key), key=key, title=title)
            self.session.add(release)
            self.session.flush()
        elif release.title != title:
            raise FormalImportError(f"release manifest drift for {key}")
        return release

    def _ensure_release_item(
        self, release: Release, rendition: Rendition, track_no: int | None, display_order: int
    ) -> None:
        item = self.session.scalar(
            select(ReleaseItem).where(
                ReleaseItem.release_id == release.id,
                ReleaseItem.rendition_id == rendition.id,
            )
        )
        if item is None:
            self.session.add(
                ReleaseItem(
                    id=deterministic_id("release-item", f"{release.key}:{rendition.id}"),
                    release_id=release.id,
                    rendition_id=rendition.id,
                    disc_no=1,
                    track_no=track_no,
                    display_order=display_order,
                )
            )
        elif item.track_no != track_no or item.display_order != display_order:
            raise FormalImportError(f"release item manifest drift for {rendition.id}")

    def _summary(self) -> ImportSummary:
        return ImportSummary(
            works=self._count(Work),
            scores=self._count(Score),
            score_revisions=self._count(ScoreRevision),
            songs=self._count(Rendition),
            albums=self._count(Release),
            release_items=self._count(ReleaseItem),
            assets=self._count(Asset),
            cos_locations=len(
                list(
                    self.session.scalars(
                        select(AssetLocation).where(AssetLocation.provider == "cos")
                    )
                )
            ),
            contributors=self._count(Contributor),
            work_credits=self._count(WorkCredit),
        )

    def _ensure_work_credits(
        self, work: Work, composers: tuple[str, ...], lyricists: tuple[str, ...]
    ) -> None:
        for role, names in (("composer", composers), ("lyricist", lyricists)):
            for position, name in enumerate(dict.fromkeys(names), 1):
                normalized = _normalized_name(name)
                contributor_id = deterministic_id("contributor", normalized)
                contributor = self.session.get(Contributor, contributor_id)
                if contributor is None:
                    contributor = Contributor(id=contributor_id, display_name=name.strip())
                    self.session.add(contributor)
                    self.session.flush()
                elif _normalized_name(contributor.display_name) != normalized:
                    raise FormalImportError(f"contributor manifest drift for {name}")
                credit_id = deterministic_id("work-credit", f"{work.id}:{role}:{normalized}")
                credit = self.session.get(WorkCredit, credit_id)
                if credit is None:
                    self.session.add(
                        WorkCredit(
                            id=credit_id,
                            work_id=work.id,
                            contributor_id=contributor.id,
                            role=role,
                            position=position,
                        )
                    )
                elif (
                    credit.work_id != work.id
                    or credit.contributor_id != contributor.id
                    or credit.role != role
                    or credit.position != position
                ):
                    raise FormalImportError(f"work credit manifest drift for {work.id}:{role}")

    def _ensure_change_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_revision: int,
        operation: str,
        work_ids: list[str],
        payload: dict[str, Any] | None = None,
    ) -> ChangeEvent:
        event = self.session.scalar(
            select(ChangeEvent).where(
                ChangeEvent.entity_type == entity_type,
                ChangeEvent.entity_id == entity_id,
                ChangeEvent.operation == operation,
            )
        )
        if event is None:
            event = ChangeEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_revision=entity_revision,
                operation=operation,
                actor_id="formal-importer",
                payload_json=payload or {},
            )
            self.session.add(event)
            self.session.flush()
        existing_work_ids = set(
            self.session.scalars(
                select(ChangeEventWork.work_id).where(
                    ChangeEventWork.event_sequence == event.sequence
                )
            )
        )
        for work_id in work_ids:
            if work_id not in existing_work_ids:
                self.session.add(
                    ChangeEventWork(event_sequence=event.sequence, work_id=work_id)
                )
        return event

    def _count(self, model: type[Any]) -> int:
        return len(list(self.session.scalars(select(model))))

    @staticmethod
    def _song_work_key(row: SongMappingRow) -> str:
        if row.mongo_work_key:
            return row.mongo_work_key
        if not row.mp3_cos_key:
            raise FormalImportError("song-only row is missing its stable COS key")
        return f"bible-song:{row.mp3_cos_key}"


def _sha256(value: Any) -> str:
    normalized = str(value).strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    return normalized


def _optional(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _optional_int(value: Any) -> int | None:
    normalized = str(value or "").strip()
    return int(normalized) if normalized else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "verified"}


def _normalize_language(value: str | None) -> str | None:
    normalized = str(value or "").strip().casefold()
    if not normalized or normalized in {"未标注", "unknown", "none"}:
        return None
    if normalized in {"english", "en"}:
        return "en"
    if normalized in {"中文", "chinese", "zh", "zh-cn", "zh-hans"}:
        return "zh-Hans"
    return value


def _names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    return tuple(str(item).strip() for item in values if str(item).strip())


def _normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
