"""Validate an isolated candidate on the existing Docker host.

在已有 Docker 主机验证隔离候选，不发布端口或修改私网映射。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import time
from pathlib import Path

SMOKE = r'''
import asyncio, json, sqlite3
from aiohttp import ClientSession, WSMsgType
async def main():
    async with ClientSession() as session:
        async with session.get('http://127.0.0.1:8390/healthz') as response:
            health = await response.json()
            assert response.status == 200 and health['runtime']['planner'] == 'offline'
        async with session.get('http://127.0.0.1:8390/api/scene.jpg') as response:
            image = await response.read()
            assert response.status == 200 and image[:2] == bytes([255, 216])
        steps = []
        async with session.ws_connect('http://127.0.0.1:8390/ws/session', origin='http://127.0.0.1:8390') as ws:
            hello = await ws.receive_json()
            assert hello['type'] == 'hello'
            await ws.send_json({'type':'text','text':'把红色方块放进盒子'})
            while True:
                message = await asyncio.wait_for(ws.receive(), 90)
                assert message.type == WSMsgType.TEXT
                event = json.loads(message.data)
                if event['type'] == 'step': steps.append(event)
                if event['type'] == 'error': raise AssertionError(event['message'])
                if event['type'] == 'reply': break
        assert len(steps) == 2 and all(step['status']=='ok' and step['operation_id'] for step in steps)
        with sqlite3.connect('/app/outputs/runtime/operations.sqlite3') as db:
            states = [row[0] for row in db.execute('SELECT state FROM operations')]
        assert states == ['RETURNED','RETURNED']
        print(json.dumps({'passed':True,'health':health,'jpeg_bytes':len(image),'steps':steps,'operation_states':states}))
asyncio.run(main())
'''


def run(command: list[str], **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--archive-sha256", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha) or not re.fullmatch(r"[0-9a-f]{64}", args.archive_sha256):
        raise ValueError("invalid immutable release identity")
    root = Path.home() / "embodied-agent"
    if root.is_symlink():
        raise ValueError("project root may not be a symlink")
    archive = root / "incoming" / f"{args.sha}.tar.gz"
    if hashlib.sha256(archive.read_bytes()).hexdigest() != args.archive_sha256:
        raise ValueError("archive digest mismatch")
    release = root / "releases" / args.sha
    release.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as source:
        source.extractall(release, filter="data")
    manifest = json.loads((release / "bundle-manifest.json").read_text())
    if manifest["commit"] != args.sha:
        raise ValueError("bundle identity mismatch")
    for name, digest in manifest["sha256"].items():
        path = (release / name).resolve()
        if not path.is_relative_to(release.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("bundle content mismatch")
    artifacts = root / "artifacts" / args.sha
    artifacts.mkdir(parents=True, exist_ok=False)
    before = run(["docker", "ps", "--format", "{{.ID}} {{.Names}}"]).stdout.splitlines()
    base = "python@sha256:b27df5841f3355e9473f9a516d38a6783b6c8dfeacaf2d14a240f443b368ddb6"
    run(["docker", "image", "inspect", base])
    print(json.dumps({"phase": "building", "commit": args.sha}), flush=True)
    with (artifacts / "build.log").open("w") as log:
        subprocess.run([
            "docker", "build", "--pull=false", "--progress=plain", "-f", str(release / "deploy/Dockerfile"),
            "--build-arg", f"BASE_IMAGE={base}", "--build-arg", f"RELEASE_SHA={args.sha}",
            "-t", f"embodied-agent:{args.sha}", str(release),
        ], check=True, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
    name = f"embodied-candidate-{args.sha[:12]}"
    run([
        "docker", "run", "-d", "--name", name, "--network", "none", "--cpus", "1", "--memory", "1536m",
        "--pids-limit", "128", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:size=536870912,mode=1777", "--tmpfs", "/app/outputs:mode=1777",
        "-e", "ASR_PROVIDER=mock", "-e", "TTS_STREAM_PROVIDER=mock", "-e", "LLM_PROVIDER=mock",
        f"embodied-agent:{args.sha}",
    ])
    print(json.dumps({"phase": "testing", "commit": args.sha}), flush=True)
    try:
        for attempt in range(40):
            ready = subprocess.run(["docker", "exec", name, "/app/.venv/bin/python", "-c",
                                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8390/healthz',timeout=2)"],
                                   capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("candidate health timeout")
        probe = run(["docker", "exec", "-i", name, "/app/.venv/bin/python", "-"], input=SMOKE, timeout=180)
        result = json.loads(probe.stdout)
        result["commit"] = args.sha
        result["image_id"] = run(["docker", "image", "inspect", f"embodied-agent:{args.sha}", "--format", "{{.Id}}"]
                                 ).stdout.strip()
        result["published"] = False
        result["runtime_limits"] = {"cpus": 1, "memory_mib": 1536, "ports": [], "network": "none"}
        (artifacts / "smoke.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        (artifacts / "console.log").write_text(logs.stdout + logs.stderr)
        run(["docker", "stop", "--time", "5", name])
        after = run(["docker", "ps", "--format", "{{.ID}} {{.Names}}"]).stdout.splitlines()
        (artifacts / "host-containers.json").write_text(json.dumps({"before": before, "after": after,
                                                                   "unchanged": sorted(before) == sorted(after)}))


if __name__ == "__main__":
    main()
