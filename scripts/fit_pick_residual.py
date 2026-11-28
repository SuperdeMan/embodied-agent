"""Fit and export a demonstration residual on the reference ACT pick policy (D044).

用独立抓取示教拟合参考 ACT 的动作误差，导出可由现有策略运行器直接加载的 ONNX。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import pyarrow.parquet as pq
from onnx import compose, helper, numpy_helper

from embodied.providers.policy import INPUT_ENV, INPUT_STATE, OUTPUT_CHUNK, POLICY_ONNX_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
JOINTS = ["Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll", "Jaw"]
OBJECT_POSE = [f"obj_cube.{axis}" for axis in ("x", "y", "z", "qw", "qx", "qy", "qz")]
STATE = "observation.state"
ENV = "observation.environment_state"
ACTION = "action"


@dataclass
class ResidualBank:
    mean: np.ndarray
    scale: np.ndarray
    features: np.ndarray
    corrections: np.ndarray
    clip: np.ndarray
    episodes: int
    frames: int
    valid_target_rows: int
    parquet_digests: dict[str, str]
    parity_inputs: list[dict[str, np.ndarray]]


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def check_dataset(dataset: Path, control_hz: float) -> dict:
    info = json.loads((dataset / "meta/info.json").read_text(encoding="utf-8"))
    if info.get("robot_type") != "sim.mujoco.so_arm100" or info.get("fps") != control_hz:
        raise ValueError("dataset robot or sampling rate differs from the reference pick policy")
    for name, order in ((STATE, JOINTS), (ACTION, JOINTS), (ENV, OBJECT_POSE)):
        feature = info["features"][name]
        if feature.get("names") != order or feature.get("shape") != [len(order)] or feature.get("dtype") != "float32":
            raise ValueError(f"unexpected physical feature layout for {name}")
    tasks = pq.read_table(dataset / "meta/tasks.parquet").to_pandas()
    if list(tasks.index) != ["skill.manip.pick"] or len(tasks["task_index"]) != 1:
        raise ValueError("fit requires a dataset segmented to skill.manip.pick")
    return {"frames": info["total_frames"], "episodes": info["total_episodes"],
            "task_index": int(tasks["task_index"].iloc[0])}


def fit_bank(dataset: Path, session: ort.InferenceSession, metadata: dict[str, str], stride: int) -> ResidualBank:
    """Fit residuals only where the expert recorded a target. 仅对专家实际记录的未来动作拟合残差。"""
    expected = check_dataset(dataset, float(metadata["control_hz"]))
    files = sorted((dataset / "data").rglob("*.parquet"))
    if not files:
        raise FileNotFoundError("dataset contains no frame parquet files")
    table = pq.read_table(files, columns=[STATE, ENV, ACTION, "episode_index", "frame_index", "task_index"])
    episodes = table["episode_index"].to_numpy()
    frame_ids = table["frame_index"].to_numpy()
    episode_ids = np.unique(episodes)
    if (not table.num_rows or table.num_rows != expected["frames"] or len(episode_ids) != expected["episodes"]
            or not np.all(table["task_index"].to_numpy() == expected["task_index"])):
        raise ValueError("dataset metadata and recorded frames disagree")
    vectors = {name: np.asarray(table[name].to_pylist(), dtype=np.float32) for name in (STATE, ENV, ACTION)}
    if not all(np.isfinite(value).all() for value in vectors.values()):
        raise ValueError("non-finite demonstration feature")

    horizon = int(metadata["chunk_size"])
    features, corrections, checks = [], [], []
    valid_target_rows = 0
    for episode_id in episode_ids:
        ids = np.flatnonzero(episodes == episode_id)
        ids = ids[np.argsort(frame_ids[ids])]
        if not np.array_equal(frame_ids[ids], np.arange(len(ids))):
            raise ValueError(f"non-contiguous frame indices in episode {episode_id}")
        state, environment, actions = (vectors[name][ids] for name in (STATE, ENV, ACTION))
        for tick in range(0, len(ids), stride):
            inputs = {INPUT_STATE: state[tick:tick + 1], INPUT_ENV: environment[tick:tick + 1]}
            prediction = session.run([OUTPUT_CHUNK], inputs)[0][0]
            if prediction.shape != (horizon, len(JOINTS)) or not np.isfinite(prediction).all():
                raise ValueError("invalid output from frozen pick policy")
            count = min(horizon, len(ids) - tick)
            # 尾段没有观测目标的位置保持零修正。 / Leave unobserved targets at the end of an episode uncorrected.
            delta = np.zeros_like(prediction)
            delta[:count] = actions[tick:tick + count] - prediction[:count]
            corrections.append(delta)
            features.append(np.concatenate((state[tick], environment[tick, :3])))
            valid_target_rows += count
            if len(checks) < 4 and tick in (0, 30, 100, 160):
                checks.append({key: value.copy() for key, value in inputs.items()})

    raw_features = np.asarray(features, dtype=np.float32)
    raw_corrections = np.asarray(corrections, dtype=np.float32)
    mean = raw_features.mean(axis=0)
    # 分别归一化关节组与位置组，设置分母下限以处理恒定维度。
    # Normalize each feature group separately and floor the scale for nearly constant dimensions.
    scale = np.maximum(raw_features.std(axis=0), np.asarray([0.02] * len(JOINTS) + [0.008] * 3, dtype=np.float32))
    scale[:len(JOINTS)] *= np.float32(np.sqrt(len(JOINTS)))
    scale[len(JOINTS):] *= np.float32(np.sqrt(3))
    clip = np.percentile(np.abs(raw_corrections), 95, axis=(0, 1)).astype(np.float32)
    return ResidualBank(
        mean, scale, (raw_features - mean) / scale, np.clip(raw_corrections, -clip, clip), clip,
        len(episode_ids), len(episodes), valid_target_rows,
        {f.relative_to(dataset).as_posix(): file_digest(f) for f in files}, checks,
    )


def compile_model(base: onnx.ModelProto, bank: ResidualBank, neighbors: int, strength: float) -> onnx.ModelProto:
    """Append the local residual to the frozen ACT graph. 将局部修正接在冻结 ACT 图的输出端。"""
    inner = compose.add_prefix(base, "act_")
    op = helper.make_node
    nodes = [op("Identity", [source.name], [renamed.name]) for source, renamed in zip(base.graph.input, inner.graph.input)]
    nodes.extend(inner.graph.node)
    constants = [
        ("xyz_idx", [0, 1, 2], "int64"), ("mean_features", bank.mean, "float32"),
        ("feature_scale", bank.scale, "float32"), ("training_features", bank.features, "float32"),
        ("training_residual", bank.corrections, "float32"), ("axis_0", [0], "int64"), ("axis_1", [1], "int64"),
        ("axes_12", [1, 2], "int64"), ("k_knn", [neighbors], "int64"), ("smoothing", 0.001, "float32"),
        ("one_f", 1.0, "float32"), ("strength", strength, "float32"),
    ]
    initializers = list(inner.graph.initializer)
    initializers.extend(numpy_helper.from_array(np.asarray(value, dtype=dtype), name=name)
                        for name, value, dtype in constants)
    nodes.extend([
        op("Gather", [INPUT_ENV, "xyz_idx"], ["current_xyz"], axis=1),
        op("Concat", [INPUT_STATE, "current_xyz"], ["raw_features"], axis=1),
        op("Sub", ["raw_features", "mean_features"], ["centered"]),
        op("Div", ["centered", "feature_scale"], ["current_features"]),
        op("Sub", ["training_features", "current_features"], ["delta"]),
        op("Mul", ["delta", "delta"], ["delta2"]),
        op("ReduceSum", ["delta2", "axis_1"], ["dist"], keepdims=0),
        op("TopK", ["dist", "k_knn"], ["near_dist", "near_idx"], axis=0, largest=0, sorted=1),
        op("Add", ["near_dist", "smoothing"], ["dist_eps"]),
        op("Div", ["one_f", "dist_eps"], ["inv"]),
        op("ReduceSum", ["inv", "axis_0"], ["sumw"], keepdims=0),
        op("Div", ["inv", "sumw"], ["w"]),
        op("Unsqueeze", ["w", "axes_12"], ["weights3"]),
        op("Gather", ["training_residual", "near_idx"], ["errors"], axis=0),
        op("Mul", ["errors", "weights3"], ["we"]),
        op("ReduceSum", ["we", "axis_0"], ["corr"], keepdims=1),
        op("Gather", ["near_dist", "axis_0"], ["nearest"], axis=0),
        op("Add", ["one_f", "nearest"], ["support"]),
        op("Div", ["strength", "support"], ["alpha"]),
        op("Mul", ["corr", "alpha"], ["shrunk"]),
        op("Add", [inner.graph.output[0].name, "shrunk"], [OUTPUT_CHUNK]),
    ])
    graph = helper.make_graph(nodes, "act_demonstration_residual", list(base.graph.input), list(base.graph.output),
                              initializer=initializers, value_info=list(inner.graph.value_info))
    model = onnx.ModelProto()
    model.CopyFrom(base)
    model.graph.CopyFrom(graph)
    return model


def verify_parity(base_session, compiled_session, bank: ResidualBank, neighbors: int, strength: float) -> float:
    """Check the exported graph against an independent NumPy calculation. 以独立 NumPy 计算校验导出图。"""
    extra = {INPUT_STATE: np.array([[0.08, 0.2, -0.12, 0.16, 0.0, 0.4]], dtype=np.float32),
             INPUT_ENV: np.array([[0.06, -0.21, 0.015, 1.0, 0, 0, 0]], dtype=np.float32)}
    maximum = 0.0
    for inputs in [*bank.parity_inputs, extra]:
        query = np.concatenate((inputs[INPUT_STATE][0], inputs[INPUT_ENV][0, :3]))
        distances = np.sum((bank.features - (query - bank.mean) / bank.scale) ** 2, axis=1)
        indices = np.argsort(distances)[:neighbors]
        weights = 1.0 / (0.001 + distances[indices])
        weights /= weights.sum()
        correction = np.sum(weights[:, None, None] * bank.corrections[indices], axis=0)
        original = base_session.run([OUTPUT_CHUNK], inputs)[0]
        expected = original + (strength / (1.0 + distances[indices[0]])) * correction
        actual = compiled_session.run([OUTPUT_CHUNK], inputs)[0]
        maximum = max(maximum, float(np.max(np.abs(expected - actual))))
        if not np.isfinite(actual).all() or maximum >= 1e-4:
            raise ValueError(f"residual ONNX parity failed: {maximum}")
    return maximum


def write_artifact(directory: Path, data: bytes, digest: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"pick-act-residual-{digest}.onnx"
    if target.exists():
        if file_digest(target) != digest:
            raise ValueError("existing content-addressed model has different bytes")
        return target
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-policy", type=Path, default=Path("outputs/policies/pick-v1.onnx"))
    parser.add_argument("--dataset", type=Path, required=True, help="LeRobot v3, pick-only, sim.mujoco.so_arm100")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/policies"))
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--neighbors", type=int, default=8)
    parser.add_argument("--strength", type=float, default=0.5)
    args = parser.parse_args()
    if args.stride < 1 or args.neighbors < 1 or not 0 < args.strength <= 1:
        parser.error("stride and neighbors must be positive, strength must be in (0, 1]")
    digest = file_digest(args.base_policy)
    # 旧导出的模型没有关节名称。用已验收的摘要确认这一特定基础模型的布局。
    # The legacy export omits joint names. Pin the qualified artifact to establish its layout.
    reference = json.loads((ROOT / "eval/release-compatibility-20261122.json").read_text(encoding="utf-8"))
    if digest != reference["pick_v1_sha256"]:
        parser.error("base policy is not the qualified ACT pick-v1")
    base = onnx.load(args.base_policy)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    base_session = ort.InferenceSession(str(args.base_policy), sess_options=options, providers=["CPUExecutionProvider"])
    metadata = dict(base_session.get_modelmeta().custom_metadata_map)
    if metadata.get("embodied.schema") != POLICY_ONNX_SCHEMA or metadata.get("task") != "skill.manip.pick":
        parser.error("base policy metadata does not match the pick contract")
    bank = fit_bank(args.dataset, base_session, metadata, args.stride)
    if args.neighbors > len(bank.features):
        parser.error("neighbor count exceeds demonstration samples")
    model = compile_model(base, bank, args.neighbors, args.strength)
    provenance = {
        "base_sha256": digest, "episodes": bank.episodes, "frames": bank.frames, "exemplars": len(bank.features),
        "valid_target_rows": bank.valid_target_rows, "stride": args.stride, "neighbors": args.neighbors,
        "strength": args.strength, "dataset_sha256": bank.parquet_digests, "feature_scale": bank.scale.tolist(),
        "residual_clip": bank.clip.tolist(),
    }
    metadata.update(source="ACT with a demonstration-fitted local residual", residual_fit=json.dumps(provenance, sort_keys=True))
    helper.set_model_props(model, metadata)
    onnx.checker.check_model(model)
    binary = model.SerializeToString()
    compiled = ort.InferenceSession(binary, sess_options=options, providers=["CPUExecutionProvider"])
    parity = verify_parity(base_session, compiled, bank, args.neighbors, args.strength)
    model_digest = hashlib.sha256(binary).hexdigest()
    output = write_artifact(args.out_dir, binary, model_digest)
    print(json.dumps({"artifact": str(output), "onnx_sha256": model_digest,
                      "parity_max_abs": parity, "fit": provenance}, indent=2))


if __name__ == "__main__":
    main()
