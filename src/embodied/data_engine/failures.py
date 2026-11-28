"""Classify evidence and index failed runs. 根据已有证据分类并索引失败局。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def failure_category(detail: str, *, aborted: bool = False) -> str:
    """Classify reported symptoms without assigning a root cause. 按已报告的症状分类，保留根因判断。"""
    detail = detail.casefold()
    if aborted:
        return "interrupted"
    for category, markers in (
        ("policy_horizon", ("horizon reached",)),
        ("observation_stale", ("semantic_target_stale", "stale_observation", "observation_expired")),
        ("guard_denied", ("guard denied", "guard_denied", "guard_halted")),
        ("timeout", ("timeout", "timed out")),
        ("confirmation_required", ("confirm_denied", "confirm_unavailable", "confirmation required")),
        ("judge_unsatisfied", ("judge predicate false",)),
        ("operator_marked", ("operator-marked",)),
    ):
        if any(marker in detail for marker in markers):
            return category
    return "unclassified"


def eval_failure_category(episode: dict) -> str:
    """Prefer the failed step over its skipped dependents. 先读取失败步骤，跳过受其影响的后继。"""
    steps = episode.get("steps", [])
    for step in steps:
        if step.get("status") == "need_confirm":
            return "confirmation_required"
        if step.get("status") == "failed":
            return failure_category(str(step.get("detail", "")))
    if steps and all(step.get("status") == "ok" for step in steps):
        return "judge_unsatisfied"
    return "unclassified"


def failure_index(roots: list[Path], category: str | None = None) -> dict:
    """Rebuild the index from current and historical files. 从新旧评测和录制文件重建只读索引。"""
    from embodied.data_engine.evaluation import RESULT_SCHEMA
    from embodied.data_engine.recorder import SCHEMA_VERSION

    files: set[Path] = set()
    issues = []
    for root in roots:
        if root.is_file():
            files.add(root.resolve())
        elif root.is_dir():
            files.update(p.resolve() for pattern in ("meta.json", "*@*-*.json") for p in root.rglob(pattern))
        else:
            issues.append({"source": str(root), "reason": "missing"})

    cases = []
    recordings = {}
    linked_recordings: set[Path] = set()
    for path in sorted(files):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema") == RESULT_SCHEMA:
                for episode in data["failures"]:
                    if episode["success"] is not False:
                        continue
                    recording = episode.get("episode_dir") or None
                    location = None
                    if recording:
                        relative = episode.get("episode_relative_to_report")
                        location = (path.parent / relative if relative else Path(recording)).resolve()
                        linked_recordings.add(location)
                    cases.append({
                        "source": str(path), "episode_dir": str(location) if location else None,
                        "task": data["task"]["id"],
                        "commit": data.get("commit"), "seed": episode.get("seed"), "index": episode["index"],
                        "category": episode.get("failure_category") or eval_failure_category(episode),
                    })
            elif data.get("schema_version") == SCHEMA_VERSION and data.get("success") is False:
                recordings[path.parent] = {
                    "source": str(path), "episode_dir": str(path.parent), "seed": data.get("seed"),
                    "category": data.get("failure_category") or failure_category(
                        data.get("detail") or "", aborted=data.get("aborted") is True),
                }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            issues.append({"source": str(path), "reason": type(exc).__name__})

    cases.extend(item for path, item in recordings.items() if path not in linked_recordings)
    if category:
        cases = [case for case in cases if case["category"] == category]
    return {"total": len(cases), "by_category": dict(Counter(case["category"] for case in cases)),
            "cases": cases, "issues": issues}
