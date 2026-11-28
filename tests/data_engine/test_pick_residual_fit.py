"""Check the offline pick fitter's layout, label bounds and exported math. 验证离线拟合的布局、标签边界与导出计算。"""

import json
import runpy
from pathlib import Path

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pd = pytest.importorskip("pandas")

builder = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/fit_pick_residual.py"))


def test_reference_layout_and_no_future_label_padding(tmp_path):
    state_key = builder["STATE"]
    env_key = builder["ENV"]
    features = {
        key: {"dtype": "float32", "shape": [len(names)], "names": names}
        for key, names in ((state_key, builder["JOINTS"]), ("action", builder["JOINTS"]), (env_key, builder["OBJECT_POSE"]))
    }
    meta = tmp_path / "meta"
    meta.mkdir()
    info = {"robot_type": "sim.mujoco.so_arm100", "fps": 50, "total_frames": 3, "total_episodes": 1, "features": features}
    (meta / "info.json").write_text(json.dumps(info), encoding="utf-8")
    pd.DataFrame({"task_index": [0]}, index=["skill.manip.pick"]).to_parquet(meta / "tasks.parquet")
    data = tmp_path / "data"
    data.mkdir()
    pq.write_table(pa.table({
        state_key: [[float(i)] * 6 for i in range(3)], env_key: [[0.0, -0.2, 0.015, 1.0, 0.0, 0.0, 0.0]] * 3,
        "action": [[float(i)] * 6 for i in (1, 2, 3)], "episode_index": [0] * 3,
        "frame_index": [0, 1, 2], "task_index": [0] * 3,
    }), data / "samples.parquet")

    helper = onnx.helper
    inputs = [helper.make_tensor_value_info(name, onnx.TensorProto.FLOAT, [1, size])
              for name, size in ((builder["INPUT_STATE"], 6), (builder["INPUT_ENV"], 7))]
    output = helper.make_tensor_value_info(builder["OUTPUT_CHUNK"], onnx.TensorProto.FLOAT, [1, 2, 6])
    constant = onnx.numpy_helper.from_array(np.zeros((1, 2, 6), dtype=np.float32), "zero")
    graph = helper.make_graph([helper.make_node("Identity", ["zero"], [output.name])], "test", inputs, [output], [constant])
    base = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    original = ort.InferenceSession(base.SerializeToString(), sess_options=options, providers=["CPUExecutionProvider"])
    bank = builder["fit_bank"](tmp_path, original, {"control_hz": "50", "chunk_size": "2"}, stride=1)
    assert bank.valid_target_rows == 5 and bank.frames == 3
    np.testing.assert_array_equal(bank.corrections[-1, 1], np.zeros(6))
    model = builder["compile_model"](base, bank, neighbors=2, strength=0.5)
    compiled = ort.InferenceSession(model.SerializeToString(), sess_options=options, providers=["CPUExecutionProvider"])
    assert builder["verify_parity"](original, compiled, bank, 2, 0.5) < 1e-5

    info["features"]["action"]["names"] = list(reversed(builder["JOINTS"]))
    (meta / "info.json").write_text(json.dumps(info), encoding="utf-8")
    with pytest.raises(ValueError, match="physical feature layout"):
        builder["check_dataset"](tmp_path, 50)
