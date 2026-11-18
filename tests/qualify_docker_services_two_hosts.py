#!/usr/bin/env python3
"""Qualify harmless stateless failover on an actual Pi and Mac.

Uses dedicated workers and a container-local readiness gate against the existing
HTTPS witness. Production supervisors remain running and ineligible for the test.
Only this run's labeled containers are removed. The test declaration is CAS-retired
because the coordinator does not implement catalog deletion.
"""
import argparse
import hashlib
import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from biomerte.docker_services import CoordinatorClient, normalize_platform, revision

PI_WORKER = "687eda586198b9e7c2bdad0c6f69731b63552e6a650e6514c663f3d472c8f714"
MAC_WORKER = "217339021b2b3d61b16660da5562889be8e57e0636bdd9a5d129e3570c67ae4b"
MANAGED = "org.biomerte.service.managed"
WORKER = "org.biomerte.service.worker"
SERVICE = "org.biomerte.service.id"
QA_LABEL = "org.biomerte.service.qualification"

CHILD = """
import json,os
from http.server import HTTPServer,BaseHTTPRequestHandler
binding={k:os.environ[e] for k,e in {
'service_id':'BIOME_SERVICE_ID','manifest_revision':'BIOME_SERVICE_REVISION',
'worker_id':'BIOME_SERVICE_WORKER','worker_incarnation':'BIOME_SERVICE_INCARNATION',
'boot_epoch':'BIOME_SERVICE_BOOT_EPOCH','grant_token':'BIOME_SERVICE_GRANT'}.items()}
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(binding).encode())
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8080),H).serve_forever()
"""

WRAPPER = """
import http.server,threading,runpy,sys,json
cfg=json.load(open('/config/runner.json'))
class H(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200 if self.path==cfg['qa_ready_path'] else 404);self.end_headers()
 def log_message(self,*args):pass
sys.path.insert(0,'/app')
server=http.server.HTTPServer(('127.0.0.1',49177),H)
threading.Thread(target=server.serve_forever,daemon=True).start()
sys.argv=['/app/docker_services.py','--config','/config/runner.json']
runpy.run_path('/app/docker_services.py',run_name='__main__')
"""

def shell(argv):
    return " ".join(shlex.quote(str(x)) for x in argv)

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def private_file(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi-host", required=True)
    parser.add_argument("--pi-image", required=True)
    parser.add_argument("--mac-image", help="Optional qualification image; production supervisor is unchanged")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--config-dir", default=os.path.expanduser("~/.local/share/biomerte-docker-services/config"))
    args = parser.parse_args()
    if not args.run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("run-id must be a safe identifier")
    source = os.path.join(ROOT, "biomerte", "docker_services.py")
    config = json.load(open(os.path.join(args.config_dir, "runner.json")))
    key_path = os.path.join(args.config_dir, "coordinator.key")
    client = CoordinatorClient(config["coordinator_url"], key_path, timeout=10)
    ssh = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlias=biome-sacred-pi",
           "-o", "UserKnownHostsFile=" + os.path.expanduser("~/.local/share/biome-mac-storage/known_hosts"),
           "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa", "-i", os.path.expanduser("~/.ssh/id_rsa")]
    destination = "pi@" + args.pi_host
    service = "qa-two-host-" + args.run_id
    workers = ["qa-pi-" + args.run_id, "qa-mac-" + args.run_id]
    names = ["biomerte-qa-pi-" + args.run_id, "biomerte-qa-mac-" + args.run_id]
    local = os.path.expanduser("~/.local/share/biomerte-docker-services/qualification/" + args.run_id)
    remote = "/home/pi/.local/share/biomerte-docker-services/qualification/" + args.run_id
    if os.path.exists(local):
        raise RuntimeError("Qualification directory already exists; inspect the prior report before rerunning")
    os.makedirs(local, mode=0o700)
    report = {"qualification": "actual Pi and Mac Docker hosts with the deployed HTTPS coordinator",
              "run_id": args.run_id, "service_id": service, "result": "running",
              "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "coordinator_url": config["coordinator_url"],
              "runner_source_sha256": hashlib.sha256(open(source, "rb").read()).hexdigest(),
              "physical_relay_tested": False, "whole_host_power_failure_tested": False,
              "failure_injection": "pause only the dedicated Pi QA supervisor; kill only the dedicated Mac test child",
              "checks": [], "cleanup": {}}
    tunnel = None
    tunnel_log = None
    admitted = False
    retired = False
    manifest = None
    begun = [False, False]

    def save_report():
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output + ".new", "w") as out:
            json.dump(report, out, sort_keys=True, indent=2)
            out.write("\n")
        os.replace(args.output + ".new", args.output)

    def record(name, **details):
        row = dict(check=name, passed=True, **details)
        report["checks"].append(row)
        save_report()
        print(json.dumps(row, sort_keys=True), flush=True)

    def process(argv, data=None, check=True):
        completed = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and completed.returncode:
            raise RuntimeError("Command failed (" + str(completed.returncode) + "): " +
                               completed.stderr.decode("utf-8", "replace")[-1400:])
        return completed.returncode, completed.stdout.decode("utf-8", "replace")

    def pi_python(code, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        return process(ssh + [destination, shell(["/usr/bin/python3", "-B", "-S", "-c", code])], data)[1]

    def docker(index, *argv, check=True):
        command = [args.docker] + list(argv) if index == 1 else ssh + [
            destination, shell(["sudo", "-n", "/usr/local/bin/docker"] + list(argv))]
        if argv and argv[0] == "logs":
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return result.stdout.decode("utf-8", "replace").strip()
        return process(command, check=check)[1].strip()

    def children(index):
        text = docker(index, "ps", "-aq", "--filter", "label=" + MANAGED + "=true",
                      "--filter", "label=" + WORKER + "=" + workers[index],
                      "--filter", "label=" + SERVICE + "=" + service)
        return text.splitlines() if text else []

    def status():
        return client.call("status", {}).get("services", {}).get(service, {})

    def sample(index):
        try:
            with urlopen("http://127.0.0.1:" + str(sample_ports[index]) + "/", timeout=2) as response:
                return response.status, json.load(response)
        except (OSError, URLError, HTTPError, ValueError):
            return None, {}

    def healthy_owner(index):
        current = status()
        code, value = sample(index)
        return ((current.get("owner") or {}).get("worker_id") == workers[index] and
                current.get("state") == "active" and code == 200 and
                value.get("worker_id") == workers[index])

    def wait_for(predicate, label, seconds=180):
        started = time.monotonic()
        next_notice = started + 20
        last_error = None
        while time.monotonic() - started < seconds:
            try:
                if predicate():
                    return time.monotonic() - started
            except (OSError, URLError, RuntimeError) as error:
                last_error = type(error).__name__
            if time.monotonic() >= next_notice:
                for host_index in range(2):
                    if begun[host_index]:
                        info = json.loads(docker(host_index, "inspect", names[host_index]))[0]
                        if not info["State"]["Running"]:
                            raise AssertionError("Qualification supervisor exited: " + names[host_index])
                print(json.dumps({"waiting_for": label, "elapsed_seconds": round(time.monotonic() - started, 1),
                                  "last_probe_error": last_error}), flush=True)
                next_notice += 20
            time.sleep(0.5)
        raise AssertionError("Qualification observation did not settle: " + label)

    def production(index):
        value = json.loads(docker(index, "inspect", "biomerte-service-runner"))[0]
        return {"id": value["Id"], "image": value["Image"], "running": value["State"]["Running"],
                "restart_policy": value["HostConfig"]["RestartPolicy"]}

    def retire():
        nonlocal retired
        if not admitted or retired:
            return
        current = status()
        if not current:
            return
        latest = current["desired_manifest"]
        disabled = dict(latest)
        disabled["readiness"] = [{"url": "http://127.0.0.1:49177/retired/" + args.run_id,
                                 "timeout_s": 1, "status": 200}]
        disabled["revision"] = revision(disabled)
        client.call("configure", {"manifest": disabled, "expected_revision": current["manifest_revision"]})
        wait_for(lambda: status().get("manifest_revision") == disabled["revision"] and
                 status().get("owner") is None, "retired test declaration drains")
        retired = True
        report["cleanup"].update(retained_declaration="retired; deletion not supported",
                                 retired_revision=disabled["revision"])
        save_report()

    try:
        before = [production(0), production(1)]
        if not all(value["running"] for value in before):
            raise AssertionError("Both actual production supervisors must be running before qualification")
        report["production_before"] = before
        versions = [json.loads(docker(index, "version", "--format", "{{json .Server}}")) for index in range(2)]
        images = [args.pi_image, args.mac_image or before[1]["image"]]
        platforms = [normalize_platform(value["Os"], value["Arch"]) for value in versions]
        if platforms != ["linux/arm/v7", "linux/arm64"]:
            raise AssertionError("Expected the actual Pi ARMv7 and Mac ARM64 engines")
        report["hosts"] = [
            {"host": "pi", "platform": platforms[0], "engine": versions[0]["Version"], "image": images[0]},
            {"host": "mac", "platform": platforms[1], "engine": versions[1]["Version"], "image": images[1]}]
        for index in range(2):
            info = json.loads(docker(index, "image", "inspect", images[index]))[0]
            if normalize_platform(info["Os"], info["Architecture"]) != platforms[index]:
                raise AssertionError("Image platform does not match its real host")
        port_pi = int(pi_python("import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()").strip())
        host_ports = [port_pi, free_port()]
        sample_ports = [free_port(), host_ports[1]]
        tunnel_log = open(os.path.join(local, "ssh-tunnel.log"), "wb")
        tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-L",
            "127.0.0.1:%s:127.0.0.1:%s" % (sample_ports[0], host_ports[0]), destination],
            stdout=subprocess.DEVNULL, stderr=tunnel_log)
        manifest = {"id": service, "images": dict(zip(platforms, images)),
                    "effect_policy": "stateless", "state_policy": "stateless",
                    "lease_ttl_s": 12, "worker_ttl_s": 18, "startup_grace_s": 45,
                    "preferred_workers": workers, "entrypoint": ["python", "-c"],
                    "command": [CHILD],
                    "readiness": [{"url": "http://127.0.0.1:49177/qa-ready/" + args.run_id,
                                   "timeout_s": 1, "status": 200}],
                    "health": {"command": ["python", "-c",
                        "import socket;s=socket.create_connection(('127.0.0.1',8080),2);s.close()"],
                        "interval_s": 2, "timeout_s": 5, "retries": 3}}
        manifest["revision"] = revision(manifest)
        if status():
            raise AssertionError("Dedicated service ID already exists")
        report["initial_revision"] = manifest["revision"]
        report["lease_ttl_s"] = manifest["lease_ttl_s"]
        save_report()
        key = open(key_path).read().strip()
        for index in range(2):
            directory = remote if index == 0 else local
            runner = {"worker_id": workers[index], "coordinator_url": config["coordinator_url"],
                      "coordinator_key_file": "/config/coordinator.key",
                      "docker_socket": "/var/run/docker.sock", "state_file": "/state/current.sqlite",
                      "manifest_files": ["/config/seed.json"] if index == 0 else [],
                      "heartbeat_interval_s": 1, "request_timeout_s": 3,
                      "docker_request_timeout_s": 60,
                      "qa_ready_path": "/qa-ready/" + args.run_id,
                      "port_overrides": {service: {"8080/tcp": [
                          {"HostIp": "127.0.0.1", "HostPort": str(host_ports[index])}]}}}
            files = {"runner.json": json.dumps(runner), "coordinator.key": key + "\n",
                     "qa-wrapper.py": WRAPPER}
            if index == 0:
                files["seed.json"] = json.dumps(manifest)
                code = """import os,json,sys
p=json.load(sys.stdin);root=p['directory']
if os.path.exists(root):raise RuntimeError('Existing QA directory; inspect before rerun')
for name in ('','/config','/state'):os.makedirs(root+name,mode=0o700)
for name,data in p['files'].items():
 fd=os.open(root+'/config/'+name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as target:target.write(data)
print('Pi qualification configuration prepared')
"""
                pi_python(code, {"directory": directory, "files": files})
            else:
                os.makedirs(directory + "/state", mode=0o700)
                for name, data in files.items():
                    private_file(directory + "/config/" + name, data.encode())
            docker(index, "run", "-d", "--name", names[index],
                   "--label", QA_LABEL + "=" + args.run_id,
                   "--log-driver", "json-file", "--log-opt", "max-size=2m", "--log-opt", "max-file=2",
                   "-v", "/var/run/docker.sock:/var/run/docker.sock",
                   "-v", directory + "/config:/config:ro", "-v", directory + "/state:/state",
                   "--entrypoint", "python", images[index], "/config/qa-wrapper.py")
            begun[index] = True
            if index == 0:
                admitted = True
                initial_seconds = wait_for(lambda: healthy_owner(0), "Pi initially owns the harmless service")
        record("initial service runs on the actual Pi", worker=workers[0],
               startup_observed_seconds=round(initial_seconds, 3))
        def standby_learned():
            try:
                with sqlite3.connect("file:" + local + "/state/current.sqlite?mode=ro", uri=True) as db:
                    row = db.execute("SELECT revision FROM manifests WHERE id=?", (service,)).fetchone()
                candidates = status().get("candidates", [])
                return row == (manifest["revision"],) and any(
                    c["worker_id"] == workers[1] and c["reason"] is None for c in candidates)
            except sqlite3.Error:
                return False
        wait_for(standby_learned, "unseeded Mac learns and prepares the exact manifest")
        record("manifest automatically reaches the actual Mac standby", revision=manifest["revision"])
        def production_excluded():
            reasons = {c["worker_id"]: c["reason"] for c in status().get("candidates", [])}
            return all(reasons.get(worker) == "dependencies_not_ready" for worker in (PI_WORKER, MAC_WORKER))
        wait_for(production_excluded, "both production supervisors reject the QA readiness gate")
        record("production supervisors are ineligible for the test",
               candidates=[c for c in status()["candidates"] if c["worker_id"] in (PI_WORKER, MAC_WORKER)])
        initial_binding = sample(0)[1]
        docker(0, "pause", names[0])
        started = time.monotonic()
        wait_for(lambda: healthy_owner(1), "Mac takes over after the Pi QA supervisor stops responding")
        elapsed = time.monotonic() - started
        stale = client.call("validate", initial_binding)
        if stale.get("valid") is not False:
            raise AssertionError("The previous Pi ownership grant still validates")
        record("actual Pi to Mac failover after an unresponsive QA supervisor",
               failover_seconds=round(elapsed, 3), active_worker=workers[1],
               old_grant_rejected=True, effect_policy="stateless")
        docker(0, "unpause", names[0])
        wait_for(lambda: not children(0), "recovered Pi removes its stale test child")
        time.sleep(3)
        if not healthy_owner(1):
            raise AssertionError("Recovered Pi displaced the healthy Mac successor")
        record("recovered Pi stays standby and removes its stale child")
        active_children = children(1)
        if len(active_children) != 1:
            raise AssertionError("Expected exactly one dedicated Mac test child")
        docker(1, "kill", active_children[0])
        started = time.monotonic()
        wait_for(lambda: healthy_owner(0), "Pi takes over after the dedicated Mac child crashes")
        record("actual Mac to Pi failover after test-child failure",
               failover_seconds=round(time.monotonic() - started, 3), active_worker=workers[0])
        retire()
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "failed"
        report["error"] = type(error).__name__ + ": " + str(error)
        print(json.dumps({"result": "failed", "error": report["error"]}), flush=True)
    finally:
        if report["result"] != "passed":
            report["runner_logs"] = {}
            for index in range(2):
                if begun[index]:
                    report["runner_logs"][names[index]] = docker(index, "logs", "--tail", "30", names[index], check=False)
        try:
            if begun[0]:
                docker(0, "unpause", names[0], check=False)
            retire()
        except Exception as error:
            report["cleanup"]["retirement_error"] = type(error).__name__ + ": " + str(error)
            report["result"] = "failed"
        for index in range(2):
            if not begun[index]:
                continue
            try:
                info = json.loads(docker(index, "inspect", names[index]))[0]
                if info["Config"].get("Labels", {}).get(QA_LABEL) != args.run_id:
                    raise AssertionError("QA supervisor ownership label changed; preserving it")
                docker(index, "rm", "-f", names[index])
                for container in children(index):
                    docker(index, "rm", "-f", container)
                report["cleanup"][("pi" if index == 0 else "mac") + "_qa_containers_removed"] = True
            except Exception as error:
                report["cleanup"][str(index) + "_error"] = type(error).__name__ + ": " + str(error)
                report["result"] = "failed"
        try:
            after = [production(0), production(1)]
            report["production_after"] = after
            if after != report.get("production_before"):
                raise AssertionError("Production supervisor identity or running state changed during qualification")
            report["cleanup"]["production_supervisors_unchanged"] = True
        except Exception as error:
            report["cleanup"]["production_check_error"] = str(error)
            report["result"] = "failed"
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait()
        if tunnel_log is not None:
            tunnel_log.close()
        for path in (local + "/config/coordinator.key",):
            if os.path.isfile(path):
                os.unlink(path)
        if begun[0]:
            try:
                pi_python("import os,sys;p=sys.argv[1] if len(sys.argv)>1 else " + repr(remote + "/config/coordinator.key") +
                          ";os.unlink(p) if os.path.isfile(p) else None")
            except Exception as error:
                report["cleanup"]["qa_key_removal_error"] = type(error).__name__
        report["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_report()
        print(json.dumps({"result": report["result"], "report": os.path.abspath(args.output),
                          "checks_passed": len(report["checks"]), "cleanup": report["cleanup"]}, sort_keys=True), flush=True)
    if report["result"] != "passed":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
