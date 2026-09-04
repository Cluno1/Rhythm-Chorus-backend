"""Atomically activate a verified issue 12 staging DB with automatic rollback.

Run as root on the production host. The script never imports into the live DB: it
stops writers, checkpoints both databases, copies+fsyncs staging to a sibling temp
file, renames the old DB into a timestamped rollback directory, and then renames
the temp file into place. The old container and image are retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXPECTED = {
    "v2_works": 107,
    "v2_scores": 77,
    "v2_score_revisions": 93,
    "v2_renditions": 73,
    "v2_releases": 1,
    "v2_release_items": 73,
    "v2_assets": 166,
    "v2_asset_locations": 166,
    "v2_contributors": 108,
    "v2_work_credits": 129,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--expected-staging-sha256", required=True)
    parser.add_argument("--rollback-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--container", default="rhythm-metadata-api")
    parser.add_argument("--staging-container", default="rhythm-metadata-api-issue12-staging")
    parser.add_argument("--host", default="10.88.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()

    live = args.live.resolve()
    staging = args.staging.resolve()
    data_dir = args.data_dir.resolve()
    rollback_root = args.rollback_root.resolve()
    validate_paths(live, staging, data_dir, rollback_root)
    if sha256_file(staging) != args.expected_staging_sha256.lower():
        raise SystemExit("staging SHA-256 does not match the approved artifact")
    verify_formal_database(staging)
    verify_legacy_database(live)
    run("docker", "image", "inspect", args.image, capture=True)
    old_image = run(
        "docker", "inspect", args.container, "--format", "{{.Image}}", capture=True
    ).strip()
    if not old_image:
        raise SystemExit("could not resolve the current production image")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rollback_dir = rollback_root / f"issue12-cutover-{stamp}"
    rollback_dir.mkdir(mode=0o750, parents=True, exist_ok=False)
    if os.stat(data_dir).st_dev != os.stat(rollback_dir).st_dev:
        raise SystemExit("data and rollback directories must be on the same filesystem")
    old_container = f"{args.container}-pre-issue12-{stamp}"
    failed_container = f"{args.container}-failed-issue12-{stamp}"
    old_db = rollback_dir / f"rhythm-v2-before-issue12-{stamp}.sqlite3"
    backup_db = rollback_dir / f"rhythm-v2-before-issue12-{stamp}.backup.sqlite3"
    failed_db = rollback_dir / f"rhythm-v2-failed-issue12-{stamp}.sqlite3"
    next_db = data_dir / f".rhythm-v2.issue12-{stamp}.next"
    original_stat = live.stat()
    old_container_renamed = False
    new_container_created = False
    database_switched = False

    try:
        stop_if_running(args.staging_container)
        run("docker", "stop", args.container)
        checkpoint(live)
        checkpoint(staging)
        verify_legacy_database(live)
        verify_formal_database(staging)
        sqlite_backup(live, backup_db)
        copy_fsync(staging, next_db)
        verify_formal_database(next_db)
        if sha256_file(next_db) != args.expected_staging_sha256.lower():
            raise RuntimeError("fsynced staging copy SHA-256 changed")

        move_sidecars(live, rollback_dir)
        os.replace(live, old_db)
        database_switched = True
        fsync_directory(rollback_dir)
        os.replace(next_db, live)
        os.chown(live, original_stat.st_uid, original_stat.st_gid)
        os.chmod(live, original_stat.st_mode & 0o777)
        fsync_file(live)
        fsync_directory(data_dir)
        run("docker", "rename", args.container, old_container)
        old_container_renamed = True
        run_new_container(args)
        new_container_created = True
        verify_api(args, live)
        run("docker", "restart", args.container)
        verify_api(args, live)
        verify_logs(args.container)

        metadata = {
            "status": "active",
            "activated_at": stamp,
            "new_image": args.image,
            "old_image_id": old_image,
            "old_container": old_container,
            "live_database": str(live),
            "live_sha256": sha256_file(live),
            "old_database": str(old_db),
            "old_database_sha256": sha256_file(old_db),
            "sqlite_backup": str(backup_db),
            "sqlite_backup_sha256": sha256_file(backup_db),
        }
        write_json_fsync(rollback_dir / "cutover.json", metadata)
        write_checksums(rollback_dir, [old_db, backup_db, rollback_dir / "cutover.json"])
        start_if_stopped(args.staging_container)
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
    except Exception as error:
        rollback_error = rollback(
            args=args,
            live=live,
            old_db=old_db,
            failed_db=failed_db,
            old_container=old_container,
            failed_container=failed_container,
            original_stat=original_stat,
            database_switched=database_switched,
            old_container_renamed=old_container_renamed,
            new_container_created=new_container_created,
        )
        message = f"cutover failed and rollback was attempted: {type(error).__name__}: {error}"
        if rollback_error:
            message += f"; rollback error: {rollback_error}"
        raise SystemExit(message) from error


def validate_paths(live: Path, staging: Path, data_dir: Path, rollback_root: Path) -> None:
    if live.name != "rhythm-v2.sqlite3":
        raise SystemExit("live database must be the explicit rhythm-v2.sqlite3 path")
    if "staging" not in staging.name.casefold() or staging == live:
        raise SystemExit("staging must be a distinct, explicitly named staging file")
    if live.parent != data_dir or staging.parent != data_dir:
        raise SystemExit("live and staging databases must be direct children of data-dir")
    if not live.is_file() or not staging.is_file() or not rollback_root.is_dir():
        raise SystemExit("live, staging, or rollback path is missing")
    if os.stat(live).st_dev != os.stat(staging).st_dev:
        raise SystemExit("live and staging databases are not on the same filesystem")


def verify_formal_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError(f"integrity check failed for {path.name}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError(f"foreign key check failed for {path.name}")
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if version != ("issue12release",):
            raise RuntimeError(f"unexpected Alembic version for {path.name}: {version}")
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in EXPECTED
        }
        if counts != EXPECTED:
            raise RuntimeError(f"formal count mismatch: {counts}")
        invariants = {
            "local": connection.execute(
                "SELECT count(*) FROM v2_asset_locations WHERE provider='local'"
            ).fetchone()[0],
            "ihope": connection.execute(
                "SELECT count(*) FROM v2_releases WHERE key='ihope' AND title='ihope'"
            ).fetchone()[0],
            "durations": connection.execute(
                "SELECT count(*) FROM v2_renditions WHERE duration_ms IS NULL OR duration_ms<=0"
            ).fetchone()[0],
        }
        if invariants != {"local": 0, "ihope": 1, "durations": 0}:
            raise RuntimeError(f"formal invariant mismatch: {invariants}")
    finally:
        connection.close()


def verify_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("legacy live DB integrity failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("legacy live DB foreign keys failed")
    finally:
        connection.close()


def verify_api(args: argparse.Namespace, live: Path) -> None:
    token = read_env(args.env_file)["RHYTHM_BOOTSTRAP_TOKEN"]
    base = f"http://{args.host}:{args.port}"
    wait_for_health(base)
    assert_unauthorized(base + "/v2/library/songs")
    albums = get_json(base + "/v2/library/albums", token)
    if len(albums["items"]) != 1 or albums["items"][0]["song_count"] != 73:
        raise RuntimeError("production album API verification failed")
    album = albums["items"][0]
    detail = get_json(base + f"/v2/library/albums/{album['id']}", token)
    if album["key"] != "ihope" or len(detail["songs"]) != 73:
        raise RuntimeError("production album detail verification failed")
    first_page = get_json(base + "/v2/library/songs?limit=50", token)
    if len(first_page["items"]) != 50 or not first_page["next_cursor"]:
        raise RuntimeError("production songs API verification failed")
    query = urllib.parse.urlencode({"limit": 50, "cursor": first_page["next_cursor"]})
    second_page = get_json(base + f"/v2/library/songs?{query}", token)
    if len(second_page["items"]) != 23 or second_page["next_cursor"] is not None:
        raise RuntimeError("production songs pagination verification failed")
    if {song["rendition_id"] for song in first_page["items"]}.intersection(
        song["rendition_id"] for song in second_page["items"]
    ):
        raise RuntimeError("production songs pagination returned duplicates")
    connection = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    try:
        asset_id = connection.execute(
            "SELECT asset_id FROM v2_score_revision_assets WHERE role='primary_musicxml' LIMIT 1"
        ).fetchone()[0]
        rendition_id = connection.execute("SELECT id FROM v2_renditions LIMIT 1").fetchone()[0]
    finally:
        connection.close()
    descriptors = [
        get_json(base + f"/v2/assets/{asset_id}/delivery", token),
        get_json(base + f"/v2/renditions/{rendition_id}/playback", token),
    ]
    for descriptor in descriptors:
        if descriptor["delivery"] != "signed_url" or not descriptor["expires_at"]:
            raise RuntimeError("production signed delivery verification failed")
        request = urllib.request.Request(descriptor["url"], headers={"Range": "bytes=0-31"})
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 206 or len(response.read()) != 32:
                raise RuntimeError("production COS range verification failed")
    verify_formal_database(live)


def assert_unauthorized(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=20)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return
        raise RuntimeError(f"unauthenticated request returned {error.code}") from error
    raise RuntimeError("unauthenticated library request was accepted")


def verify_logs(container: str) -> None:
    result = subprocess.run(
        ["docker", "logs", container],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    logs = result.stdout.casefold()
    forbidden = ("/content", "q-signature", "q-key-time", "cos_secret", " 500 ", " 502 ", " 503 ")
    found = [marker for marker in forbidden if marker in logs]
    if found:
        raise RuntimeError(f"production log safety verification failed: {found}")


def rollback(
    *,
    args: argparse.Namespace,
    live: Path,
    old_db: Path,
    failed_db: Path,
    old_container: str,
    failed_container: str,
    original_stat: os.stat_result,
    database_switched: bool,
    old_container_renamed: bool,
    new_container_created: bool,
) -> str | None:
    try:
        if new_container_created and container_exists(args.container):
            stop_if_running(args.container)
            run("docker", "rename", args.container, failed_container)
        if database_switched:
            move_sidecars(live, failed_db.parent, prefix=failed_db.name)
            if live.exists():
                os.replace(live, failed_db)
            os.replace(old_db, live)
            os.chown(live, original_stat.st_uid, original_stat.st_gid)
            os.chmod(live, original_stat.st_mode & 0o777)
            fsync_file(live)
            fsync_directory(live.parent)
        if old_container_renamed:
            run("docker", "rename", old_container, args.container)
        start_if_stopped(args.container)
        wait_for_health(f"http://{args.host}:{args.port}")
        start_if_stopped(args.staging_container)
        return None
    except Exception as error:  # noqa: BLE001  # pragma: no cover - emergency path
        return f"{type(error).__name__}: {error}"


def run_new_container(args: argparse.Namespace) -> None:
    run(
        "docker",
        "run",
        "-d",
        "--name",
        args.container,
        "--network",
        "host",
        "--env-file",
        str(args.env_file),
        "-e",
        "RHYTHM_ENVIRONMENT=production",
        "-e",
        "RHYTHM_DATABASE_PATH=/data/rhythm.sqlite3",
        "-e",
        "RHYTHM_V2_DATABASE_PATH=/data/rhythm-v2.sqlite3",
        "-e",
        "RHYTHM_LOCAL_OBJECT_ROOT=/data/objects",
        "-v",
        f"{args.data_dir.resolve()}:/data",
        "--restart",
        "unless-stopped",
        "--init",
        "--memory",
        "256m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        args.image,
        "uvicorn",
        "rhythm_metadata_api.main:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
    )


def checkpoint(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise RuntimeError(f"WAL checkpoint busy for {path.name}: {result}")
    finally:
        connection.close()


def sqlite_backup(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("final SQLite backup integrity failed")
    finally:
        src.close()
        dst.close()
    fsync_file(target)


def copy_fsync(source: Path, target: Path) -> None:
    if target.exists():
        raise RuntimeError(f"temporary target already exists: {target}")
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def move_sidecars(database: Path, directory: Path, prefix: str | None = None) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            destination = directory / f"{prefix or database.name}{suffix}"
            os.replace(sidecar, destination)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=20
    ) as response:
        return json.loads(response.read())


def wait_for_health(base: str) -> None:
    last_error: Exception | None = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        time.sleep(1)
    raise RuntimeError(f"health check timed out: {type(last_error).__name__}")


def stop_if_running(container: str) -> None:
    if container_exists(container) and container_running(container):
        run("docker", "stop", container)


def start_if_stopped(container: str) -> None:
    if container_exists(container) and not container_running(container):
        run("docker", "start", container)


def container_exists(container: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def container_running(container: str) -> bool:
    return (
        run("docker", "inspect", container, "--format", "{{.State.Running}}", capture=True)
        == "true"
    )


def run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_fsync(path: Path, value: dict[str, str]) -> None:
    with path.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def write_checksums(directory: Path, paths: list[Path]) -> None:
    value = "".join(f"{sha256_file(path)}  {path.name}\n" for path in paths)
    target = directory / "SHA256SUMS"
    with target.open("x", encoding="utf-8") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


if __name__ == "__main__":
    main()
