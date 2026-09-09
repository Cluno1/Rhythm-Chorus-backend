# v2 architecture

## Product boundary

Rhythm is a single-user private Work catalog. The supported path is:

```text
Android Work page
  → Arrangement
      → Score → immutable ScoreRevision → MusicXML Asset
      → Rendition → RenditionAsset → protected audio/MIDI Asset
```

The Android app does not scan MediaStore, accept arbitrary file/content URIs for playback, or use third-party music servers. SAF remains an import mechanism only: bytes are hashed, uploaded, validated, registered as an Asset, and then linked to a ScoreRevision or Rendition. Media3 may cache a registered Asset for offline use without changing its Asset identity.

## Modules

- `api/v2`: HTTP/auth/header mapping only.
- `application`: transactional use cases, validation, idempotency and events.
- `domain/v2`: strict request/response contracts and domain errors.
- `infrastructure/db`: SQLAlchemy models, session factory and Alembic migrations.
- `infrastructure/storage`: streamed local object storage behind `AssetStorage`.

The service is a modular monolith. SQLite is appropriate for the current single-node writer; move to PostgreSQL only if multi-instance writes become real.

## Identity rules

- Work ID identifies the abstract piece; titles are not unique.
- Arrangement ID identifies voicing/key/part structure.
- Score ID identifies one score branch; ScoreRevision is immutable.
- Rendition ID identifies the playable performance/take/practice realization.
- Asset ID and SHA-256 identify bytes, not a Work or a performance.
- WAV and MP3 of one take are two Assets of one Rendition.

## Consistency

- Work, Arrangement, Score and Rendition have independent revisions.
- Writes use `If-Match: "rev-N"`; stale writes return 412 with `current_etag`.
- Creates use `Idempotency-Key`; reuse with a different body returns 409.
- Business mutation and `change_events` append share one database transaction.
- File upload is two-phase so a large transfer never holds a database transaction.
- `GET /works/{id}/bundle` is versioned by the latest event sequence and supports 304.

## Storage and playback

Asset content, source provenance and physical location are separate tables. Local storage uses `sha256/<prefix>/<hash>`. `GET /v2/renditions/{id}/playback` chooses stream/mix/master/midi and returns the authenticated Asset URL, immutable cache key, hash ETag and Range capability. The content endpoint remains bearer-protected.

## Multilingual lyrics

Work, Score and Rendition each own an independent localized lyric set. The existing `lyrics` text remains the default-language value so old clients remain compatible. `lyrics_language` identifies that value with a BCP 47-style tag, and `lyrics_translations` stores the other variants as an ordered JSON array of `{language, lyrics}` objects.

The library projection resolves lyrics per language in Rendition → preferred Score → Work order. A higher-priority entity replaces only languages it supplies; it does not hide lower-priority translations in other languages. Duplicate language tags and a translation that repeats the default language are rejected at the domain boundary.

## Deferred modules

Metadata suggestions, deletes/GC, derived jobs and a writable runtime COS adapter are intentionally outside the implemented core.
