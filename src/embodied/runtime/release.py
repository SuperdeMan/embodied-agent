"""Release compatibility preflight, full v2 ledger backup, and atomic disaster recovery (D037).

发行兼容与维护态回滚预检、全七表 v2 账本在线备份及受损账本认证原子恢复（D037）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

from embodied.contracts.release import (
    LedgerBackupManifest,
    ModelBundle,
    ReleaseBundle,
    ReleasePreflightReport,
    TargetEnvironment,
)
from embodied.runtime.operations import LedgerUnavailable, take_ledger_lock
from embodied.runtime.task_schema import LEGACY_TABLES, inspect_schema

V2_TABLES = ("schema_version", *LEGACY_TABLES, "task_plans", "task_events")
V1_TABLES = ("schema_version", *LEGACY_TABLES)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_child(root: Path, rel: str) -> Path | None:
    """Resolve relative path strictly inside root; return None on traversal or absolute path.

    将相对路径严格限定在根目录内部，遇绝对路径或越界穿透返回 None。
    """
    if not rel or rel.startswith(("/", "\\")):
        return None
    base = root.resolve()
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base):
        return None
    return candidate


def full_ledger_digest(db: sqlite3.Connection, version: int) -> tuple[str, dict[str, int]]:
    """Compute deterministic SHA-256 across all tables for schema v1 (5 tables) or v2 (7 tables).

    计算 schema v1（5 表）或 v2（7 表）全部行内容的确定性 SHA-256 摘要与行数统计。
    """
    tables = V2_TABLES if version == 2 else V1_TABLES
    content: dict[str, list[list]] = {}
    counts: dict[str, int] = {}
    for table in tables:
        rows = [list(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
        # 以完整 JSON 序列稳定排序，兼容单列与复合主键（如 task_plans(task_id, revision)）。
        # Sort by full canonical JSON row representation so composite PKs (e.g. task_plans) are strictly deterministic.
        rows.sort(key=lambda r: json.dumps(r, ensure_ascii=False, allow_nan=False))
        content[table] = rows
        counts[table] = len(rows)
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), counts


def inspect_ledger_state(path: str | Path, *, require_exclusive_lock: bool = False) -> dict:
    """Inspect ledger schema, integrity, active operations, and full 7-table digest without mutating it.

    只读检查账本 schema 形状、SQLite 完整性、活跃/未确认停止操作及全七表摘要。
    """
    target = Path(path).resolve(strict=True)
    lock = take_ledger_lock(Path(str(target) + ".lock")) if require_exclusive_lock else None
    try:
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            integrity = [row[0] for row in db.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                raise ValueError(f"sqlite_integrity_failed:{integrity}")
            schema_info = inspect_schema(db)
            version = int(schema_info["version"])
            all_sha, all_counts = full_ledger_digest(db, version)
            unconfirmed_stops = int(
                db.execute(
                    "SELECT count(*) FROM attempts WHERE state='OUTCOME_UNKNOWN' AND stop_state != 'MOTION_STOPPED_OBSERVED'"
                ).fetchone()[0]
            )
            return {
                "version": version,
                "all_tables_sha256": all_sha,
                "legacy_history_sha256": schema_info["history_sha256"],
                "row_counts": all_counts,
                "running_operations": int(schema_info["running"]),
                "unconfirmed_stops": unconfirmed_stops,
                "integrity_ok": True,
            }
    finally:
        if lock is not None:
            lock.close()


def verify_model_compatibility(
    bundle: ModelBundle,
    target: TargetEnvironment,
    *,
    artifact_root: Path | None = None,
    verify_onnx_metadata: bool = True,
) -> list[str]:
    """Validate physical/semantic compatibility of one ModelBundle against a target environment.

    校验单个模型包与目标环境的物理及语义兼容性；相同张量维度不等于物理兼容。
    """
    reasons: list[str] = []
    prefix = f"model:{bundle.model_id}"
    if bundle.robot_id != target.robot_id:
        reasons.append(f"{prefix}:robot_id_mismatch")
    if bundle.units != target.units:
        reasons.append(f"{prefix}:units_mismatch")
    if bundle.action_semantics not in target.supported_action_semantics:
        reasons.append(f"{prefix}:action_semantics_unsupported")
    if bundle.normalization_mode not in target.supported_normalization_modes:
        reasons.append(f"{prefix}:normalization_mode_unsupported")
    if bundle.backend not in target.supported_backends:
        reasons.append(f"{prefix}:backend_unsupported")
    if bundle.action_semantics in ("absolute_joint_targets", "delta_joint_targets"):
        if bundle.joint_order != target.joint_order:
            reasons.append(f"{prefix}:joint_order_mismatch")

    if artifact_root is not None:
        artifact = _safe_child(artifact_root, bundle.artifact_path)
        if artifact is None:
            reasons.append(f"{prefix}:artifact_path_traversal")
        elif not artifact.is_file():
            reasons.append(f"{prefix}:artifact_missing")
        else:
            actual_sha = _file_sha256(artifact)
            if actual_sha != bundle.artifact_sha256:
                reasons.append(f"{prefix}:artifact_sha256_mismatch")
            elif verify_onnx_metadata and artifact.suffix.lower() == ".onnx":
                try:
                    from embodied.providers.policy import OnnxPolicy

                    meta = OnnxPolicy(artifact).meta
                    if (
                        meta.state_dim != bundle.state_dim
                        or meta.env_dim != bundle.env_dim
                        or meta.action_dim != bundle.action_dim
                        or meta.chunk_size != bundle.chunk_size
                        or abs(meta.control_hz - bundle.control_hz) > 1e-6
                    ):
                        reasons.append(f"{prefix}:onnx_metadata_mismatch")
                except RuntimeError:
                    # 无 policy 依赖组（onnxruntime）时跳过图元数据解析，仍强制保留 SHA-256 与语义检查。
                    # Skip ONNX runtime inspection when the optional policy group is absent; SHA-256 & semantic gates still hold.
                    pass
                except Exception:
                    reasons.append(f"{prefix}:onnx_artifact_invalid")
    return reasons


def preflight_release(
    bundle: ReleaseBundle,
    target: TargetEnvironment,
    *,
    ledger_path: str | Path | None = None,
    action: Literal["activate", "rollback"] = "activate",
    current_bundle: ReleaseBundle | None = None,
    bundle_root: str | Path | None = None,
    require_exclusive_lock: bool = True,
) -> ReleasePreflightReport:
    """Run fail-closed maintenance-mode preflight for release activation or rollback.

    执行维护态版本激活或回滚的 fail-closed 兼容预检；未经验证组合永不标为 rollback_safe。
    """
    reasons: list[str] = []
    root = None if bundle_root is None else Path(bundle_root).resolve()

    if bundle.robot_id != target.robot_id:
        reasons.append("robot_id_mismatch")
    if target.driver_firmware_version not in bundle.driver_firmware_compat:
        reasons.append("driver_firmware_incompatible")
    if bundle.asset_digest != target.asset_digest:
        reasons.append("asset_digest_mismatch")
    if bundle.stop_profile_digest != target.stop_profile_digest:
        reasons.append("stop_profile_digest_mismatch")

    if root is not None:
        for rel, expected in bundle.config_digests.items():
            resolved = _safe_child(root, rel)
            if resolved is None:
                reasons.append(f"config_path_traversal:{rel}")
            elif not resolved.is_file():
                reasons.append(f"config_file_missing:{rel}")
            elif _file_sha256(resolved) != expected:
                reasons.append(f"config_digest_mismatch:{rel}")

    for model in bundle.model_bundles:
        reasons.extend(verify_model_compatibility(model, target, artifact_root=root))

    ledger_version: int | None = None
    ledger_sha = ""
    if ledger_path is not None:
        try:
            state = inspect_ledger_state(ledger_path, require_exclusive_lock=require_exclusive_lock)
        except LedgerUnavailable:
            reasons.append("ledger_locked_by_live_owner")
        except (FileNotFoundError, sqlite3.DatabaseError, ValueError):
            reasons.append("ledger_corrupt_or_unsupported_schema")
        else:
            ledger_version = int(state["version"])
            ledger_sha = str(state["all_tables_sha256"])
            if not (bundle.min_ledger_schema <= ledger_version <= bundle.max_ledger_schema):
                reasons.append(
                    f"ledger_schema_incompatible:v{ledger_version}_not_in_[{bundle.min_ledger_schema},{bundle.max_ledger_schema}]"
                )
            if state["running_operations"] > 0:
                reasons.append("active_running_operations")
            if state["unconfirmed_stops"] > 0:
                reasons.append("unconfirmed_stop_operations_pending")

    # 显式验证回滚目标；未声明为已验证回滚对的组合拒绝回滚且绝不标 rollback_safe。
    # Require explicit verified_rollback_targets; unverified pairs refuse rollback and never report rollback_safe.
    if action == "rollback":
        if current_bundle is None:
            reasons.append("rollback_missing_current_bundle")
        else:
            verified = (
                bundle.commit_sha in current_bundle.verified_rollback_targets
                or bundle.release_id in current_bundle.verified_rollback_targets
            )
            if not verified:
                reasons.append("unverified_rollback_target")

    allowed = len(reasons) == 0
    if not allowed:
        rollback_safe = False
    elif action == "rollback":
        rollback_safe = True
    else:
        rollback_safe = (
            current_bundle is not None
            and (
                current_bundle.commit_sha in bundle.verified_rollback_targets
                or current_bundle.release_id in bundle.verified_rollback_targets
            )
            and (
                ledger_version is None
                or (current_bundle.min_ledger_schema <= ledger_version <= current_bundle.max_ledger_schema)
            )
        )

    return ReleasePreflightReport(
        action=action,
        target_release_id=bundle.release_id,
        target_commit_sha=bundle.commit_sha,
        current_release_id=None if current_bundle is None else current_bundle.release_id,
        allowed=allowed,
        rollback_safe=rollback_safe,
        ledger_schema_version=ledger_version,
        ledger_all_tables_sha256=ledger_sha,
        reasons=tuple(reasons),
    )


def backup_ledger(
    path: str | Path,
    backup_dir: str | Path,
    *,
    source_release: str,
) -> tuple[Path, LedgerBackupManifest]:
    """Create an authenticated online SQLite backup + full-table manifest under exclusive lock.

    在独占锁下使用 SQLite 在线备份接口生成认证备份文件与全表校验清单；拒绝活跃操作或受损账本。
    """
    target = Path(path).resolve(strict=True)
    folder = Path(backup_dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    lock = take_ledger_lock(Path(str(target) + ".lock"))
    temp_path: Path | None = None
    try:
        state = inspect_ledger_state(target, require_exclusive_lock=False)
        if state["running_operations"] > 0:
            raise ValueError("cannot_backup_with_running_operations")
        stamp = uuid4().hex[:12]
        final_backup = folder / f"{target.stem}.v{state['version']}-{stamp}.sqlite3"
        temp_path = folder / f".{final_backup.name}.tmp"
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2)) as src:
            with closing(sqlite3.connect(temp_path)) as dst:
                src.backup(dst)
                dst.commit()
        os.replace(temp_path, final_backup)
        temp_path = None
        backup_sha = _file_sha256(final_backup)
        # 复核落盘备份文件与原库全表摘要一致。 / Re-verify persisted backup matches source all-tables digest.
        backup_state = inspect_ledger_state(final_backup, require_exclusive_lock=False)
        if backup_state["all_tables_sha256"] != state["all_tables_sha256"]:
            raise ValueError("backup_digest_verification_failed")
        manifest = LedgerBackupManifest(
            source_release=source_release,
            schema_version=int(state["version"]),
            backup_file=final_backup.name,
            backup_sha256=backup_sha,
            all_tables_sha256=str(state["all_tables_sha256"]),
            legacy_history_sha256=str(state["legacy_history_sha256"]),
            row_counts=dict(state["row_counts"]),
            created_unix=time.time(),
        )
        manifest_path = folder / f"{final_backup.name}.manifest.json"
        manifest_tmp = folder / f".{manifest_path.name}.tmp"
        manifest_tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(manifest_tmp, manifest_path)
        return final_backup, manifest
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        lock.close()


def restore_ledger_backup(
    target_path: str | Path,
    backup_path: str | Path,
    *,
    expected_sha256: str,
    expected_all_tables_sha256: str | None = None,
) -> dict:
    """Atomically restore a corrupted/rolled-back ledger strictly from an authenticated backup.

    在独占锁下先核验备份文件哈希、SQLite 完整性与全表摘要，全部通过后原子替换目标账本。
    """
    target = Path(target_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = Path(backup_path).resolve(strict=True)
    lock = take_ledger_lock(Path(str(target) + ".lock"))
    temp_path: Path | None = None
    try:
        actual_sha = _file_sha256(backup)
        if actual_sha != expected_sha256:
            raise ValueError("backup_sha256_mismatch")
        backup_state = inspect_ledger_state(backup, require_exclusive_lock=False)
        if expected_all_tables_sha256 is not None and backup_state["all_tables_sha256"] != expected_all_tables_sha256:
            raise ValueError("backup_all_tables_sha256_mismatch")

        temp_path = target.parent / f".{target.name}.restore-{uuid4().hex[:12]}.tmp"
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True, timeout=2)) as src:
            with closing(sqlite3.connect(temp_path)) as dst:
                src.backup(dst)
                dst.commit()
        # 独占锁下清理可能残留的旧 WAL/SHM 旁路文件，避免恢复后的主库误重放损坏帧。
        # Clear any stale SQLite WAL/SHM sidecars under the exclusive lock so corrupted WAL frames cannot replay onto the restored DB.
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        os.replace(temp_path, target)
        temp_path = None

        restored_state = inspect_ledger_state(target, require_exclusive_lock=False)
        if restored_state["all_tables_sha256"] != backup_state["all_tables_sha256"]:
            raise ValueError("post_restore_digest_mismatch")
        return {
            "restored": True,
            "target": str(target),
            "backup": str(backup),
            "backup_sha256": actual_sha,
            **restored_state,
        }
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        lock.close()
