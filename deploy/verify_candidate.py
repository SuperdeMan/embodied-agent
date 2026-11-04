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
import asyncio, json, sqlite3, threading, time
from aiohttp import ClientSession, WSMsgType
async def main():
    async with ClientSession() as session:
        async with session.get('http://127.0.0.1:8390/healthz') as response:
            health = await response.json()
            assert response.status == 200 and health['runtime']['planner'] == 'offline'
        frame_attempts=[]
        frame_deadline=time.monotonic()+6
        while True:
            async with session.get('http://127.0.0.1:8390/api/scene.jpg') as response:
                image = await response.read()
                frame_attempts.append({'status':response.status,'bytes':len(image)})
                if response.status==200:
                    assert image[:2] == bytes([255,216])
                    break
                assert response.status==503 and time.monotonic()<frame_deadline
            await asyncio.sleep(0.25)
        steps = []
        preview_statuses=[]
        preview_stop=asyncio.Event()
        async def preview():
            while not preview_stop.is_set():
                async with session.get('http://127.0.0.1:8390/api/scene.jpg') as frame:
                    data=await frame.read()
                    assert frame.status in (200,503)
                    if frame.status==200: assert data[:2]==bytes([255,216])
                    preview_statuses.append(frame.status)
                await asyncio.sleep(0.2)
        async with session.ws_connect('http://127.0.0.1:8390/ws/session', origin='http://127.0.0.1:8390') as ws:
            hello = await ws.receive_json()
            assert hello['type'] == 'hello'
            polling=asyncio.create_task(preview())
            try:
                await asyncio.sleep(0.02)
                await ws.send_json({'type':'text','text':'把红色方块放进盒子'})
                while True:
                    message = await asyncio.wait_for(ws.receive(), 90)
                    assert message.type == WSMsgType.TEXT
                    event = json.loads(message.data)
                    if event['type'] == 'step': steps.append(event)
                    if event['type'] == 'error': raise AssertionError(event['message'])
                    if event['type'] == 'reply': break
            finally:
                preview_stop.set()
                await asyncio.wait_for(polling,3)
        assert 200 in preview_statuses
        assert len(steps) == 2 and all(step['status']=='ok' and step['operation_id'] for step in steps)
        task_id=event['task_id']
        assert task_id and all(step['task_id']==task_id for step in steps)
        with sqlite3.connect('/app/outputs/runtime/operations.sqlite3') as db:
            assert db.execute('SELECT version FROM schema_version').fetchone()[0]==2
            assert db.execute('SELECT count(*) FROM task_plans').fetchone()[0]==1
            journal_before=db.execute('SELECT count(*) FROM task_events').fetchone()[0]
            states = [row[0] for row in db.execute('SELECT state FROM operations')]
            evidence = [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM operation_events WHERE event_type='effect_observed' ORDER BY rowid")]
        assert states == ['RETURNED','RETURNED']
        from embodied.contracts.effect import EffectEvidence
        assert len(evidence) == 2 and all(e['verdict']=='sat' and EffectEvidence.model_validate(e).intact() for e in evidence)
        async with session.get('http://127.0.0.1:8390/api/tasks/'+task_id+'?assess=1') as response:
            task=await response.json()
            assert response.status==200 and task['workflow']=='FINISHED'
            assert task['read_only'] and not task['automatic_resume_allowed'] and len(task['steps'])==2
        with sqlite3.connect('/app/outputs/runtime/operations.sqlite3') as db:
            assert db.execute('SELECT count(*) FROM task_events').fetchone()[0]==journal_before
            assert db.execute('SELECT count(*) FROM operations').fetchone()[0]==2
        from embodied.runtime.observation import ObservationLane, ObservationUnavailable
        lane=ObservationLane(0.1)
        release=threading.Event()
        heartbeat=asyncio.create_task(asyncio.sleep(0.01))
        started=time.monotonic()
        try:
            try:
                await lane.call(lambda: release.wait(2))
                raise AssertionError('blocking observation unexpectedly returned')
            except ObservationUnavailable:
                assert lane.busy and heartbeat.done()
            try:
                await lane.call(lambda: True)
                raise AssertionError('busy observation was queued')
            except ObservationUnavailable:
                assert lane.started==1
            latency=time.monotonic()-started
            assert latency < 1.0
        finally:
            release.set()
            while lane.busy:
                await asyncio.sleep(0.001)
        assert await lane.call(lambda: 'fresh')=='fresh'
        print(json.dumps({'passed':True,'health':health,'jpeg_bytes':len(image),'steps':steps,
                          'operation_states':states,'effect_verdicts':[e['verdict'] for e in evidence],
                          'task_journal':{'schema_version':2,'task_id':task_id,'events':journal_before,'read_only':True},
                          'frame_attempts':frame_attempts,
                          'concurrent_preview_statuses':preview_statuses,
                          'bounded_observation':{'passed':True,'timeout_probe_s':latency,'queued_replacements':0}}))
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
        try:
            probe = run(["docker", "exec", "-i", name, "/app/.venv/bin/python", "-"], input=SMOKE, timeout=180)
        except subprocess.CalledProcessError as exc:
            (artifacts / "smoke-failure.json").write_text(json.dumps({
                "returncode": exc.returncode, "stdout": exc.stdout[-20000:], "stderr": exc.stderr[-20000:],
            }))
            raise
        result = json.loads(probe.stdout)
        try:
            computation = run(["docker", "exec", name, "/app/.venv/bin/python", "/app/scripts/compute_smoke.py",
                               "--out", "/app/outputs/compute-smoke.json"], timeout=120)
        except subprocess.CalledProcessError as exc:
            (artifacts / "compute-failure.json").write_text(json.dumps({
                "returncode": exc.returncode, "stdout": exc.stdout[-20000:], "stderr": exc.stderr[-20000:],
            }))
            raise
        result["isolated_compute"] = json.loads(computation.stdout)
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
