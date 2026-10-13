"""Real HTTP + real linear client acceptance on the current OS, optionally frozen."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "node-agent"))
from lastcore_node_agent.credentials import NodeCredentials, acquire_state_lock, save_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="lastcore-native-linear-") as folder:
        run = Path(folder)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0));port=sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        admin, registration = "native-admin-"+os.urandom(24).hex(), "native-register-"+os.urandom(24).hex()
        environment = dict(os.environ, CONTROLLER_ADMIN_TOKEN=admin, CONTROLLER_REGISTRATION_TOKEN=registration,
                           CONTROLLER_DATABASE_PATH=str(run/"controller.sqlite3"), CONTROLLER_REAL_JOBS_ENABLED="false")
        state = run/"state"
        config = run/"node.env"
        config.write_text(f"NODE_AGENT_CONTROLLER_URL={origin}\nNODE_AGENT_ENDPOINT=http://127.0.0.1:9000\n"
                          f"NODE_AGENT_STATE_DIR={state}\nNODE_AGENT_AVAILABLE_MEMORY_MB=128\n"
                          "NODE_AGENT_NAME=native-linear-test\nNODE_AGENT_HEARTBEAT_INTERVAL=0.2\nNODE_AGENT_POLL_INTERVAL=0.1\n")
        config.chmod(0o600)
        try:
            with (run/"server.log").open("w") as output:
                server = subprocess.Popen([sys.executable,"-m","uvicorn","app.main:create_app","--factory",
                                           "--host","127.0.0.1","--port",str(port)],cwd=ROOT/"controller-server",
                                          env=environment,stdout=output,stderr=output)
            processes.append(server)
            with httpx.Client(base_url=origin,timeout=5,trust_env=False) as api:
                deadline = time.monotonic()+12
                while True:
                    try:
                        if api.get("/healthz").status_code==200:break
                    except httpx.ConnectError:pass
                    if server.poll() is not None or time.monotonic()>deadline:raise RuntimeError("Controller startup failed")
                    time.sleep(.1)
                response=api.post("/v1/nodes/register",headers={"Authorization":"Bearer "+registration},json={
                    "name":"native-linear-test","platform":sys.platform,"endpoint":"http://127.0.0.1:9000",
                    "available_memory_mb":128,"compute_weight":1})
                response.raise_for_status();identity=response.json()
                save_credentials(state,NodeCredentials(identity["node_id"],identity["node_token"],30))
                base=[args.client] if args.client else [sys.executable,str(ROOT/"edge-client/linear_edge.py")]
                with (run/"client.log").open("w") as output:
                    client=subprocess.Popen(base+["--env-file",str(config),"--bind-existing-origin",origin],stdout=output,stderr=output)
                processes.append(client)
                node_id=identity["node_id"]
                request=dict(model_id="lastcore/linear-demo-v1",num_layers=2,layer_memory_mb=1,max_nodes=1,
                             timeout_seconds=30,input_vector=[1,2,3,4],weight_seed=7,target_node_id=node_id)
                response=api.post("/v1/tasks",headers={"Authorization":"Bearer "+admin},json=request)
                response.raise_for_status();job=response.json()
                deadline=time.monotonic()+20
                while job["status"] in ("queued","running") and time.monotonic()<deadline:
                    if client.poll() is not None:raise RuntimeError("Native client stopped before completion")
                    time.sleep(.1)
                    response=api.get("/v1/tasks/"+job["id"],headers={"Authorization":"Bearer "+admin})
                    response.raise_for_status();job=response.json()
                expected=[0.9999684130511179,-0.9047475044778314]
                assert job["status"]=="completed" and len(job["result"])==2
                difference=max(abs(a-b) for a,b in zip(job["result"],expected))
                assert difference<1e-12
                published=json.loads((state/"last-linear-result.json").read_text())
                assert published["task_id"]==job["id"] and published["result"]==job["result"]
                token=identity["node_token"]
                assert api.get("/v1/nodes",headers={"Authorization":"Bearer "+token}).status_code==401
                evidence=dict(passed=True,platform=sys.platform,frozen_client=bool(args.client),
                              scope="real local HTTP Controller and native-platform linear client; not physical user-device or LLM proof",
                              task_status=job["status"],assignment_status=job["assignments"][0]["status"],
                              result=job["result"],max_abs_diff=difference,node_cannot_list_fleet=True,
                              node_identity_preserved=True,own_result_published=True)
        finally:
            for process in reversed(processes):
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=8)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        lock=acquire_state_lock(state)
        assert lock is not None;lock.close()
        evidence["owned_processes_exited"]=all(p.poll() is not None for p in processes)
        evidence["state_lock_reacquired"]=True
        Path(args.out).write_text(json.dumps(evidence,indent=2)+"\n")
        print(json.dumps(evidence,indent=2))


if __name__=="__main__":main()
