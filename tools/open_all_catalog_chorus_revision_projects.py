#!/usr/bin/env python3
"""Ensure one chorus project per Score and one timeline per selectable revision."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class TimelinePlan:
    work_id: str
    arrangement_id: str
    score_id: str
    score_revision_id: str
    revision_no: int
    timeline_hash: str


def request_json(
    api_base: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], int]:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(api_base.rstrip("/") + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response), response.status
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error


def catalog_works(api_base: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        path = "/v2/library/score-works?limit=200"
        if cursor:
            path += "&cursor=" + quote(cursor, safe="")
        page, _ = request_json(api_base, token, "GET", path)
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if len({item["work_id"] for item in items}) != len(items):
        raise RuntimeError("score Catalog returned duplicate Work IDs")
    return items


def revision_chain(
    api_base: str,
    token: str,
    score_id: str,
    published_revision_id: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    revision_id: str | None = published_revision_id
    while revision_id is not None:
        if revision_id in seen:
            raise RuntimeError(f"Score {score_id} has a cyclic revision chain")
        seen.add(revision_id)
        revision, _ = request_json(
            api_base,
            token,
            "GET",
            f"/v2/score-revisions/{revision_id}",
        )
        if revision["score_id"] != score_id:
            raise RuntimeError(f"Revision {revision_id} does not belong to Score {score_id}")
        items.append(revision)
        revision_id = revision.get("based_on_revision_id")
    return items


def build_plan(
    api_base: str,
    token: str,
    works: list[dict[str, Any]],
) -> tuple[list[TimelinePlan], int, int, int, int]:
    plans: list[TimelinePlan] = []
    existing_projects = 0
    existing_timelines = 0
    selectable = 0
    missing_project_keys: set[tuple[str, str]] = set()
    for work in works:
        work_id = work["work_id"]
        chorus, _ = request_json(api_base, token, "GET", f"/v2/works/{work_id}/chorus")
        projects_by_score = {project["score_id"]: project for project in chorus["projects"]}
        existing_projects += len(projects_by_score)
        for option in work["score_options"]:
            score_id = option["score_id"]
            project = projects_by_score.get(score_id)
            existing_revision_ids = {
                timeline["score_revision_id"] for timeline in (project or {}).get("timelines", [])
            }
            seen_revision_ids: set[str] = set()
            for revision in revision_chain(
                api_base,
                token,
                score_id,
                option["revision_id"],
            ):
                revision_id = revision["id"]
                if revision_id in seen_revision_ids:
                    continue
                seen_revision_ids.add(revision_id)
                selectable += 1
                if revision_id in existing_revision_ids:
                    existing_timelines += 1
                    continue
                if project is None:
                    missing_project_keys.add((work_id, score_id))
                primary_assets = [
                    asset for asset in revision["assets"] if asset["role"] == "primary_musicxml"
                ]
                if len(primary_assets) != 1:
                    raise RuntimeError(
                        f"ScoreRevision {revision_id} does not have one primary MusicXML"
                    )
                timeline_hash = primary_assets[0]["sha256"].lower()
                if len(timeline_hash) != 64 or any(
                    char not in "0123456789abcdef" for char in timeline_hash
                ):
                    raise RuntimeError(
                        f"ScoreRevision {revision_id} has an invalid MusicXML SHA-256"
                    )
                plans.append(
                    TimelinePlan(
                        work_id=work_id,
                        arrangement_id=option["arrangement_id"],
                        score_id=score_id,
                        score_revision_id=revision_id,
                        revision_no=revision["revision_no"],
                        timeline_hash=timeline_hash,
                    )
                )
    return (
        plans,
        existing_projects,
        existing_timelines,
        len(missing_project_keys),
        selectable,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://10.88.0.1:8010")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--title", default="在线合唱")
    args = parser.parse_args()

    token = os.environ.get("RHYTHM_BOOTSTRAP_TOKEN", "")
    if not token:
        raise RuntimeError("RHYTHM_BOOTSTRAP_TOKEN is required")

    works = catalog_works(args.api_base, token)
    (
        plans,
        existing_projects,
        existing_timelines,
        planned_projects,
        selectable,
    ) = build_plan(args.api_base, token, works)
    ensured_timelines = 0
    if args.apply:
        for plan in plans:
            _, status = request_json(
                args.api_base,
                token,
                "POST",
                f"/v2/works/{plan.work_id}/chorus-projects",
                {
                    "arrangement_id": plan.arrangement_id,
                    "alignment_score_revision_id": plan.score_revision_id,
                    "timeline_hash": plan.timeline_hash,
                    "title": args.title,
                    "status": "open",
                },
                f"issue80-ensure-timeline-{plan.work_id}-{plan.score_id}-{plan.score_revision_id}",
            )
            if status not in {200, 201}:
                raise RuntimeError(
                    f"unexpected create status {status} for Work {plan.work_id} "
                    f"revision {plan.revision_no}"
                )
            ensured_timelines += 1

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "plan",
                "catalog_works": len(works),
                "selectable_revisions": selectable,
                "existing_projects": existing_projects,
                "existing_timelines": existing_timelines,
                "planned_projects": planned_projects,
                "planned_timelines": len(plans),
                "ensured_timelines": ensured_timelines,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
