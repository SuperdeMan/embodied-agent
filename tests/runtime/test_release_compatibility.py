"""ReleaseBundle/ModelBundle compatibility preflight and v2 ledger downgrade/recovery tests (D037).

发行包与模型语义兼容预检、维护态门禁、v2 账本新写入后降级拒止/受控回滚，以及受损账本认证恢复测试（D037）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from embodied.cli import main as cli_main
from embodied.contracts.operation import OperationIdentity
from embodied.contracts.release import ModelBundle, ReleaseBundle, TargetEnvironment
from embodied.runtime.operations import LedgerUnavailable, OperationLedger
from embodied.runtime.release import (
    backup_ledger,
    inspect_ledger_state,
    preflight_release,
    restore_ledger_backup,
)
from embodied.runtime.task_schema import upgrade_ledger
from embodied.skills.manifest import ParamSpec, SkillManifest
from embodied.skills.registry import SkillRegistry, SkillResult

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_COMMIT_OLD = "1" * 40
_COMMIT_NEW = "2" * 40
_JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def _target(**changes) -> TargetEnvironment:
    base = dict(
        robot_id="sim.mujoco.so_arm100",
        asset_digest=_SHA_A,
        stop_profile_digest=_SHA_B,
        joint_order=_JOINTS,
        units="m_rad",
        supported_backends=("onnxruntime_cpu",),
        supported_action_semantics=("absolute_joint_targets",),
        supported_normalization_modes=("baked_in_graph",),
        driver_firmware_version="sim_reference_v1",
    )
    return TargetEnvironment(**(base | changes))


def _model(artifact_path: str = "models/pick-v1.bin", artifact_sha256: str = _SHA_C, **changes) -> ModelBundle:
    base = dict(
        model_id="pick-v1",
        skill_name="skill.manip.pick",
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        robot_id="sim.mujoco.so_arm100",
        joint_order=_JOINTS,
        units="m_rad",
        action_semantics="absolute_joint_targets",
        normalization_mode="baked_in_graph",
        state_dim=6,
        env_dim=7,
        action_dim=6,
        chunk_size=16,
        control_hz=50.0,
        backend="onnxruntime_cpu",
        precision="fp32",
        license="Apache-2.0",
        eval_task_id="tabletop_pick_place@v1",
        eval_win_rate=29 / 30,
        eval_evidence_sha256=_SHA_D,
    )
    return ModelBundle(**(base | changes))


def _release(**changes) -> ReleaseBundle:
    base = dict(
        release_id="rel-v2-new",
        commit_sha=_COMMIT_NEW,
        runtime_version="0.2.0",
        robot_id="sim.mujoco.so_arm100",
        driver_firmware_compat=("sim_reference_v1",),
        min_ledger_schema=2,
        max_ledger_schema=2,
        asset_digest=_SHA_A,
        stop_profile_digest=_SHA_B,
        config_digests={},
        skills_catalog_sha256=_SHA_C,
        model_bundles=(),
        eval_evidence_sha256=_SHA_D,
        verified_rollback_targets=(),
    )
    return ReleaseBundle(**(base | changes))


def _write_v2_ledger_with_history(db_path: Path) -> SkillRegistry:
    """Populate a real v2 SQLite ledger via SkillRegistry with plans, task events, operations, attempts, and audit events.

    经由 SkillRegistry 构建含任务计划、任务事件、持久操作、尝试记录与操作者审计事件的真实 v2 SQLite 账本。
    """
    registry = SkillRegistry(ledger=OperationLedger(db_path), robot_id="sim.mujoco.so_arm100")

    async def pick(object: str = "obj_cube"):
        return SkillResult(ok=True, detail=f"{object} grasped")

    registry.register(
        SkillManifest(
            name="skill.manip.pick",
            description="pick",
            params={"object": ParamSpec(type="string", default="obj_cube", required=False)},
        ),
        pick,
    )
    registry.record_task_plan(
        {
            "task_id": "task-v2-1",
            "revision": 1,
            "goal": "pick cube",
            "steps": [{"id": "s1", "skill": "skill.manip.pick", "params": {"object": "obj_cube"}}],
        }
    )
    report = registry.get_task("task-v2-1")
    op_id = report["plans"][0]["steps"][0]["operation_id"]
    registry.record_task_intent("task-v2-1", 1, "s1", {"object": "obj_cube"})
    identity = OperationIdentity(task_id="task-v2-1", operation_id=op_id, plan_revision=1)
    rec = registry.ledger.begin(
        identity,
        "sim.mujoco.so_arm100",
        "skill.manip.pick",
        "req-hash-1",
        ("arm", "gripper", "workspace"),
        1,
    )
    registry.ledger.finish(
        rec.operation_id,
        "RETURNED",
        {"ok": True, "detail": "obj_cube grasped"},
        "NOT_REQUESTED",
    )
    with registry.ledger._db:
        registry.ledger._event(
            rec.operation_id,
            rec.attempt_id,
            "rpc_operator_action_finished",
            {"scope": "read_only", "status": "completed"},
        )
    registry.record_task_result(
        "task-v2-1",
        1,
        "s1",
        {"status": "ok", "operation_id": op_id, "attempt_id": rec.attempt_id, "invocation_state": "RETURNED"},
    )
    registry.finish_task("task-v2-1", interrupted=False)
    return registry


@pytest.mark.parametrize(
    "changes",
    [
        {"api_version": "embodied.dev/v99"},
        {"commit_sha": "shortsha"},
        {"min_ledger_schema": 3, "max_ledger_schema": 2},
        {"asset_digest": "not-a-sha"},
        {"config_digests": {"/etc/passwd": _SHA_A}},
        {"hardware_verified": True},
    ],
)
def test_release_bundle_strict_contract_rejects_invalid_fields(changes):
    """ReleaseBundle rejects unknown versions, bad digests, inverted schema ranges, and hardware_verified=True.

    ReleaseBundle 拒绝未知契约版本、非法摘要、倒置 schema 区间、绝对路径配置与伪造真机认证标志。
    """
    with pytest.raises(ValidationError):
        _release(**changes)


def test_model_bundle_same_shape_different_semantics_is_rejected(tmp_path):
    """Matching state/action dims never implies physical compatibility: swapped joints, delta mode, or tampered weights fail.

    输入输出维度完全一致仍不能跳过物理语义校验：关节顺序互换、动作语义差异、错误本体/后端或权重哈希篡改一律拒绝。
    """
    weights = tmp_path / "models" / "pick-v1.bin"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"simulated-onnx-weights-v1")
    digest = hashlib.sha256(weights.read_bytes()).hexdigest()

    cfg = tmp_path / "config" / "profile.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"mode":"sim"}\n', encoding="utf-8")
    cfg_digest = hashlib.sha256(cfg.read_bytes()).hexdigest()

    target = _target()

    # 1. 合法模型包 + 配置摘要 -> 通过。 / Valid model + config digest -> allowed.
    ok_bundle = _release(
        config_digests={"config/profile.json": cfg_digest},
        model_bundles=(_model("models/pick-v1.bin", digest),),
    )
    rep_ok = preflight_release(ok_bundle, target, bundle_root=tmp_path)
    assert rep_ok.allowed is True
    assert rep_ok.rollback_safe is False  # 未提供 current_bundle 或已验证回滚对时绝不标 rollback_safe。

    # 2. 同为 6 维但关节序互换 -> 拒绝。 / Same 6 dims with swapped joint_order -> refused.
    swapped_joints = (_JOINTS[1], _JOINTS[0], *_JOINTS[2:])
    rep_swap = preflight_release(
        _release(model_bundles=(_model("models/pick-v1.bin", digest, joint_order=swapped_joints),)),
        target,
        bundle_root=tmp_path,
    )
    assert not rep_swap.allowed
    assert "model:pick-v1:joint_order_mismatch" in rep_swap.reasons

    # 3. 同为 6 维但动作语义为增量目标 / 外部归一化 / CUDA 后端 -> 全部拒绝。
    # Same dims but delta_joint_targets / external_stats / unsupported backend -> all refused.
    rep_sem = preflight_release(
        _release(
            model_bundles=(
                _model(
                    "models/pick-v1.bin",
                    digest,
                    action_semantics="delta_joint_targets",
                    normalization_mode="external_stats",
                    backend="onnxruntime_cuda",
                    robot_id="real.so_arm101",
                ),
            )
        ),
        target,
        bundle_root=tmp_path,
    )
    assert not rep_sem.allowed
    for expected in (
        "model:pick-v1:robot_id_mismatch",
        "model:pick-v1:action_semantics_unsupported",
        "model:pick-v1:normalization_mode_unsupported",
        "model:pick-v1:backend_unsupported",
    ):
        assert expected in rep_sem.reasons

    # 4. 权重或配置文件篡改、路径穿越 -> 拒绝。 / Tampered weights/config or path traversal -> refused.
    weights.write_bytes(b"tampered-weights")
    cfg.write_text('{"mode":"tampered"}\n', encoding="utf-8")
    rep_tamper = preflight_release(ok_bundle, target, bundle_root=tmp_path)
    assert not rep_tamper.allowed
    assert "model:pick-v1:artifact_sha256_mismatch" in rep_tamper.reasons
    assert "config_digest_mismatch:config/profile.json" in rep_tamper.reasons

    rep_escape = preflight_release(
        _release(model_bundles=(_model("../outside.bin", digest),)),
        target,
        bundle_root=tmp_path,
    )
    assert not rep_escape.allowed
    assert "model:pick-v1:artifact_path_traversal" in rep_escape.reasons


def test_maintenance_mode_blocks_live_owner_running_and_unconfirmed_stop(tmp_path):
    """Activation and rollback refuse live ledger lock owners, RUNNING operations, and unconfirmed stops.

    存在活跃进程账本锁、RUNNING 操作或未确认停止隔离尝试时，激活与回滚预检一律拒绝。
    """
    db_path = tmp_path / "operations.sqlite3"
    reg = _write_v2_ledger_with_history(db_path)
    target = _target()
    bundle = _release()

    # 1. 活跃进程持有 OS 锁 -> 拒绝。 / Live process holding OS lock -> refused.
    rep_locked = preflight_release(bundle, target, ledger_path=db_path, require_exclusive_lock=True)
    assert not rep_locked.allowed
    assert "ledger_locked_by_live_owner" in rep_locked.reasons

    # 2. 插入一条 RUNNING 操作并释放锁 -> 仍因 RUNNING 拒绝。 / RUNNING operation -> refused.
    op_run = OperationIdentity(task_id="task-v2-1", operation_id="op-running", plan_revision=1)
    rec_run = reg.ledger.begin(op_run, "sim.mujoco.so_arm100", "skill.manip.pick", "h-run", ("arm",), 2)
    reg.close()

    rep_running = preflight_release(bundle, target, ledger_path=db_path)
    assert not rep_running.allowed
    assert "active_running_operations" in rep_running.reasons

    # 3. 模拟崩溃恢复：RUNNING 转为 OUTCOME_UNKNOWN / STOP_UNCONFIRMED -> 仍拒绝激活。
    # Crash recovery turns RUNNING into OUTCOME_UNKNOWN / STOP_UNCONFIRMED -> still refused.
    reopened = OperationLedger(db_path)
    recovered = reopened.get(rec_run.operation_id)
    assert recovered is not None and recovered.state == "OUTCOME_UNKNOWN" and recovered.stop_state == "STOP_UNCONFIRMED"
    reopened.close()

    rep_unconfirmed = preflight_release(bundle, target, ledger_path=db_path)
    assert not rep_unconfirmed.allowed
    assert "unconfirmed_stop_operations_pending" in rep_unconfirmed.reasons


def test_v2_writes_refuse_v1_downgrade_and_unverified_rollback_but_allow_verified_v2_rollback(tmp_path):
    """After new v2 writes, v1-only downgrade & unverified rollbacks fail closed; verified v2 rollback preserves all 7 tables.

    新 v2 写入后，降级到仅支持 v1 的旧版或未验证回滚目标一律拒绝；经验证的 v2 回滚通过且全七表摘要完全保留。
    """
    db_path = tmp_path / "operations.sqlite3"
    reg = _write_v2_ledger_with_history(db_path)
    reg.close()

    before = inspect_ledger_state(db_path, require_exclusive_lock=True)
    assert before["version"] == 2
    assert before["row_counts"]["task_plans"] == 1
    assert before["row_counts"]["task_events"] >= 3
    assert before["row_counts"]["operations"] == 1

    target = _target()
    current_v2 = _release(
        release_id="rel-d037",
        commit_sha=_COMMIT_NEW,
        verified_rollback_targets=(_COMMIT_OLD,),
    )
    v1_only_target = _release(
        release_id="rel-v1-legacy",
        commit_sha=_COMMIT_OLD,
        min_ledger_schema=1,
        max_ledger_schema=1,
    )
    unverified_v2_target = _release(
        release_id="rel-unverified",
        commit_sha="3" * 40,
        min_ledger_schema=2,
        max_ledger_schema=2,
    )
    verified_v2_target = _release(
        release_id="rel-cb05aee",
        commit_sha=_COMMIT_OLD,
        min_ledger_schema=2,
        max_ledger_schema=2,
    )

    # 1. v1-only 降级必须因 schema 不兼容拒绝。 / v1-only downgrade refused as schema-incompatible.
    rep_v1 = preflight_release(
        v1_only_target, target, ledger_path=db_path, action="rollback", current_bundle=current_v2,
    )
    assert not rep_v1.allowed
    assert not rep_v1.rollback_safe
    assert any(r.startswith("ledger_schema_incompatible:v2_not_in_[1,1]") for r in rep_v1.reasons)

    # 2. 未在 verified_rollback_targets 登记的回滚目标必须拒绝。 / Unverified rollback target refused.
    rep_unverified = preflight_release(
        unverified_v2_target, target, ledger_path=db_path, action="rollback", current_bundle=current_v2,
    )
    assert not rep_unverified.allowed
    assert not rep_unverified.rollback_safe
    assert "unverified_rollback_target" in rep_unverified.reasons

    # 3. 经验证的同构 v2 目标允许回滚且全七表摘要一致。 / Verified v2 rollback allowed with identical 7-table digest.
    rep_v2 = preflight_release(
        verified_v2_target, target, ledger_path=db_path, action="rollback", current_bundle=current_v2,
    )
    assert rep_v2.allowed is True
    assert rep_v2.rollback_safe is True
    assert rep_v2.ledger_schema_version == 2
    assert rep_v2.ledger_all_tables_sha256 == before["all_tables_sha256"]

    # 4. 校验正向激活时 rollback_safe 仅在双向兼容与显式登记时为真。
    # Forward activation reports rollback_safe=True only when current_bundle is verified & schema-compatible.
    rep_fwd = preflight_release(
        current_v2, target, ledger_path=db_path, action="activate", current_bundle=verified_v2_target,
    )
    assert rep_fwd.allowed is True
    assert rep_fwd.rollback_safe is True


def test_backup_corrupt_ledger_and_authenticated_atomic_restore(tmp_path):
    """Authenticated backup restores a corrupted v2 ledger atomically; tampered backup or live lock is refused.

    经认证备份可原子恢复受损的 v2 账本并逐行还原全七表摘要；篡改备份或存在活跃锁时拒绝恢复。
    """
    db_path = tmp_path / "operations.sqlite3"
    reg = _write_v2_ledger_with_history(db_path)
    expected_op_id = reg.get_task("task-v2-1")["plans"][0]["steps"][0]["operation_id"]
    reg.close()

    backup_dir = tmp_path / "backups"
    backup_path, manifest = backup_ledger(db_path, backup_dir, source_release="rel-d037")
    assert backup_path.is_file()
    assert (backup_dir / f"{backup_path.name}.manifest.json").is_file()
    assert manifest.schema_version == 2
    assert manifest.row_counts["task_plans"] == 1
    assert manifest.row_counts["task_events"] >= 3

    # 模拟异常损坏（截断 SQLite 文件并写入垃圾字节）。 / Simulate corrupt ledger file.
    db_path.write_bytes(b"corrupted-sqlite-header-truncated")
    rep_corrupt = preflight_release(_release(), _target(), ledger_path=db_path)
    assert not rep_corrupt.allowed
    assert "ledger_corrupt_or_unsupported_schema" in rep_corrupt.reasons

    # 篡改期望哈希必须拒绝恢复且不破坏现状。 / Wrong expected SHA-256 refuses restore.
    with pytest.raises(ValueError, match="backup_sha256_mismatch"):
        restore_ledger_backup(db_path, backup_path, expected_sha256="0" * 64)

    # 认证恢复原子重建完整 v2 七表账本。 / Authenticated restore atomically recovers intact v2 ledger.
    restored = restore_ledger_backup(
        db_path,
        backup_path,
        expected_sha256=manifest.backup_sha256,
        expected_all_tables_sha256=manifest.all_tables_sha256,
    )
    assert restored["restored"] is True
    assert restored["all_tables_sha256"] == manifest.all_tables_sha256

    # 恢复后 SkillRegistry + OperationLedger 正常打开，既有计划、事件、操作与审计全量保留，且持锁期间拒绝再次恢复。
    # Reopened runtime retains all history and blocks concurrent restore while holding its lock.
    reopened = SkillRegistry(ledger=OperationLedger(db_path), robot_id="sim.mujoco.so_arm100")
    try:
        task = reopened.get_task("task-v2-1")
        assert task is not None and task["found"] is True
        assert task["workflow"] == "FINISHED"
        op = reopened.ledger.get(expected_op_id)
        assert op is not None and op.state == "RETURNED"
        with pytest.raises(LedgerUnavailable):
            restore_ledger_backup(db_path, backup_path, expected_sha256=manifest.backup_sha256)
    finally:
        reopened.close()


def test_v1_upgrade_then_new_v2_writes_and_cli_release_check_end_to_end(tmp_path, capsys):
    """Upgrade a v1 ledger to v2, append new v2 writes, and exercise `embodied release-check` CLI end-to-end.

    从真实 v1 账本升级至 v2 并追加新写入，随后端到端验证 `embodied release-check` 预检、备份、损坏拒止与恢复。
    """
    from contextlib import closing

    from embodied.runtime.operations import _SCHEMA

    db_path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(_SCHEMA)
    up = upgrade_ledger(db_path, apply=True, backup_dir=tmp_path / "upgrade-backups")
    assert up["applied"] is True and up["after"]["version"] == 2

    reg = _write_v2_ledger_with_history(db_path)
    reg.close()

    target_file = tmp_path / "target.json"
    curr_file = tmp_path / "current.json"
    prev_file = tmp_path / "prev.json"
    bad_file = tmp_path / "v1only.json"
    target_file.write_text(_target().model_dump_json(), encoding="utf-8")
    curr_file.write_text(_release(release_id="rel-new", verified_rollback_targets=(_COMMIT_OLD,)).model_dump_json(), encoding="utf-8")
    prev_file.write_text(_release(release_id="rel-prev", commit_sha=_COMMIT_OLD).model_dump_json(), encoding="utf-8")
    bad_file.write_text(_release(release_id="rel-v1", commit_sha=_COMMIT_OLD, min_ledger_schema=1, max_ledger_schema=1).model_dump_json(), encoding="utf-8")

    # 1. CLI 拒绝 v1-only 降级（返回码 2）。 / CLI refuses v1-only downgrade with exit code 2.
    rc_bad = cli_main([
        "release-check", "--bundle", str(bad_file), "--target", str(target_file),
        "--current-bundle", str(curr_file), "--action", "rollback", "--ledger", str(db_path),
    ])
    assert rc_bad == 2
    out_bad = json.loads(capsys.readouterr().out)
    assert out_bad["allowed"] is False and out_bad["rollback_safe"] is False

    # 2. CLI 放行已验证 v2 回滚（返回码 0）。 / CLI allows verified v2 rollback with exit code 0.
    rc_ok = cli_main([
        "release-check", "--bundle", str(prev_file), "--target", str(target_file),
        "--current-bundle", str(curr_file), "--action", "rollback", "--ledger", str(db_path),
    ])
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["allowed"] is True and out_ok["rollback_safe"] is True

    # 3. CLI 在线备份 -> 损坏账本 -> CLI 认证恢复。 / CLI online backup -> corrupt -> authenticated CLI restore.
    bdir = tmp_path / "cli-backups"
    rc_bk = cli_main([
        "release-check", "--ledger", str(db_path), "--backup-dir", str(bdir), "--source-release", "rel-new",
    ])
    assert rc_bk == 0
    bk_info = json.loads(capsys.readouterr().out)
    db_path.write_bytes(b"broken")

    rc_rs = cli_main([
        "release-check", "--ledger", str(db_path), "--restore-from", bk_info["backup_path"],
        "--expected-sha256", bk_info["manifest"]["backup_sha256"],
        "--expected-all-tables-sha256", bk_info["manifest"]["all_tables_sha256"],
    ])
    assert rc_rs == 0
    rs_info = json.loads(capsys.readouterr().out)
    assert rs_info["restored"] is True
    assert rs_info["all_tables_sha256"] == bk_info["manifest"]["all_tables_sha256"]


def test_unknown_future_schema_and_corrupt_backup_are_refused(tmp_path):
    """Unknown schema version (e.g. v99) fails closed in preflight, backup, and restore.

    未知未来 schema 版本（如 v99）在预检、在线备份与备份恢复阶段一律 fail-closed 拒绝。
    """
    from contextlib import closing

    from embodied.runtime.operations import _SCHEMA

    db_path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(db_path)) as db:
        db.executescript(_SCHEMA)
        db.execute("UPDATE schema_version SET version=99")
        db.commit()

    rep = preflight_release(
        _release(min_ledger_schema=2, max_ledger_schema=99),
        _target(),
        ledger_path=db_path,
    )
    assert not rep.allowed
    assert "ledger_corrupt_or_unsupported_schema" in rep.reasons

    with pytest.raises(ValueError, match="unsupported schema"):
        backup_ledger(db_path, tmp_path / "bk", source_release="rel-future")

    future_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
    target_db = tmp_path / "active.sqlite3"
    with pytest.raises(ValueError, match="unsupported schema"):
        restore_ledger_backup(target_db, db_path, expected_sha256=future_sha)
    assert not target_db.exists()
