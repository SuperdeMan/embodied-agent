"""Failure indexing for old reports and v0 recordings. 失败索引兼容历史评测和 v0 录制。"""

import json

from embodied.data_engine.failures import failure_index
from embodied.data_engine.recorder import EpisodeRecorder


def test_legacy_eval_link_deduplication_and_unrecorded_failures(tmp_path):
    recorder = EpisodeRecorder(tmp_path / "episodes")
    linked = recorder.start("tabletop", "sim.test").finish(False, detail="no detail")
    manual = recorder.start("teleop", "sim.test").finish(False, detail="operator-marked")
    recorder.start("in progress", "sim.test")
    report = tmp_path / "tabletop@v1-previous.json"
    report.write_text(json.dumps({
        "schema": "eval.result/v1", "task": {"id": "tabletop@v1"}, "commit": "abc123",
        "failures": [
            {"index": 14, "seed": 1, "success": False, "episode_dir": str(linked),
             "steps": [{"status": "failed", "detail": "policy pick failed (horizon reached)"},
                       {"status": "skipped", "detail": "dependency failed"}]},
            {"index": 15, "seed": 1, "success": False, "episode_dir": "", "steps": []},
        ],
    }), encoding="utf-8")
    indexed = failure_index([tmp_path, tmp_path / "episodes"])
    assert indexed["total"] == 3 and indexed["issues"] == []
    assert indexed["by_category"] == {"operator_marked": 1, "policy_horizon": 1, "unclassified": 1}
    assert failure_index([tmp_path], "policy_horizon")["cases"][0]["index"] == 14
    assert json.loads((manual / "meta.json").read_text(encoding="utf-8"))["failure_category"] == "operator_marked"


def test_unreadable_report_is_visible_and_does_not_hide_other_cases(tmp_path):
    EpisodeRecorder(tmp_path).start("task", "sim.test").abort("interrupted")
    (tmp_path / "tabletop@v1-broken.json").write_text("{", encoding="utf-8")
    result = failure_index([tmp_path, tmp_path / "missing"])
    assert result["by_category"] == {"interrupted": 1}
    assert {item["reason"] for item in result["issues"]} == {"missing", "JSONDecodeError"}
