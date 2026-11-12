#!/usr/bin/env python3
"""Native Biome motion capabilities backed by the observer's durable model.

This worker owns its own inbox and ledger. It never owns a drive lease, shares
another provider's inbox, or depends on a public/local HTTP prediction route.
"""
import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

CONTRACTS = {
 "robot.motion.training.start:1": dict(name="robot.motion.training.start",version=1,
    inputs={"session_id":"str","label":"str?","metadata":"obj?"},outputs={"result":"obj"},synchronous=False,
    description="Start an idempotent named manual-driving training session. Records applied controls and sensor observations through the passive observer; no driving is commanded. A repeated session_id never reopens a stopped session. Inspect recording, recorder health and accepted sample counts before assuming training is working."),
 "robot.motion.training.stop:1": dict(name="robot.motion.training.stop",version=1,
    inputs={"session_id":"str"},outputs={"result":"obj"},synchronous=False,
    description="Stop recording a named manual-driving session idempotently. Already-received observations drain before status becomes stopped. Does not stop the physical rover; use its normal stop control."),
 "robot.motion.training.status:1": dict(name="robot.motion.training.status",version=1,
    inputs={"session_id":"str?"},outputs={"result":"obj"},synchronous=False,
    description="Read manual training session, captured event counts, accepted distance/yaw samples, rejection reasons, observer heartbeat and sensor/control freshness. Omit session_id for the most recent session. Recording is not physical calibration."),
 "robot.motion.training.read:1": dict(name="robot.motion.training.read",version=1,
    inputs={"session_id":"str","after_id":"int?","limit":"int?"},outputs={"result":"obj"},synchronous=False,
    description="Read a page of retained manual-driving controls and sensor metadata with sample acceptance/rejection evidence and initial model snapshot. No video is recorded. Follow next_after_id; active/draining sessions are mutable. Never drives."),
 "robot.motion.plan:1": dict(name="robot.motion.plan",version=1,
    inputs={"target":"obj","initial_state":"obj?","constraints":"obj?","model_revision":"int?"},
    outputs={"result":"obj"},synchronous=False,
    description="Inverse motion estimate: target {distance_m,yaw_deg} to pulse timings using the learned history-dependent model. Distance is signed travelled distance; yaw is unwrapped heading change in degrees, positive left. Optional initial_state, model_revision and constraints bound the scenario/search. Returns pulse plan, forward prediction, residual, uncertainty and support; untrained models return insufficient_calibration without invented timings. Read-only; never drives or guarantees stopping/obstacle clearance."),
 "robot.motion.model:1": dict(name="robot.motion.model",version=1,inputs={"include_parameters":"bool?"},
    outputs={"result":"obj"},synchronous=False,
    description="Read the adaptive rover motion model, retained steering/motion state, observation support and freshness. Steering persists across neutral/straight pulses. include_parameters exports the portable estimator snapshot; no movement is commanded."),
 "robot.motion.predict:1": dict(name="robot.motion.predict",version=1,
    inputs={"pulses":"list","initial_state":"obj?","step_s":"num?","model_revision":"int?"},
    outputs={"result":"obj"},synchronous=False,
    description="Estimate a planned pulse sequence using retained steering, prior drive/speed/yaw and adaptive response. Each pulse is {mask,duration_s}; mask bits1 forward,2 reverse,4 left,8 right. Neutral/straight never assumes centered wheels. Optional initial_state and model_revision pin scenario assumptions. Read-only: never drives. Unsupported/untrained outputs are null; uncertainty is observational, not a certified bound.")
}

class QueryError(ValueError):
    def __init__(self,code,message):
        self.code,self.message=code,message
        super().__init__(message)

class ModelBackend:
    def __init__(self,database):
        self.database=Path(database)

    def read(self):
        try:
            db=sqlite3.connect(self.database.resolve().as_uri()+"?mode=ro",uri=True)
            try:
                db.execute("BEGIN")
                row=db.execute("SELECT payload,updated_utc FROM model WHERE id=1").fetchone()
                latest=db.execute("SELECT payload,received_monotonic FROM events WHERE kind='vision' ORDER BY id DESC LIMIT 1").fetchone()
            finally:db.close()
        except sqlite3.Error as error:
            raise QueryError("model_unavailable","The observational model store is unavailable") from error
        if row is None:raise QueryError("model_unavailable","No model snapshot is available")
        try:saved=json.loads(row[0])
        except (ValueError,TypeError) as error:
            raise QueryError("model_invalid","The stored model snapshot is not valid JSON") from error
        capture_age=None
        if latest:
            sample=json.loads(latest[0])
            interval=(sample.get("frame") or {}).get("capture_host_monotonic_interval_s")
            if isinstance(interval,list) and len(interval)==2:
                try:capture_age=time.monotonic()-float(interval[0])
                except (ValueError,TypeError):pass
        freshness=dict(model_updated_utc=row[1],model_age_s=max(0.,time.time()-row[1]),
            capture_age_s=capture_age,
            live_state_available=capture_age is not None and 0<=capture_age<=2. and 0<=time.time()-row[1]<=2.)
        return saved,freshness,hashlib.sha256(row[0].encode()).hexdigest()

    def query(self,contract,inputs):
        if contract not in CONTRACTS:raise QueryError("unsupported_contract","Unknown motion capability")
        if not isinstance(inputs,dict):raise QueryError("invalid_request","Inputs must be an object")
        if contract.startswith("robot.motion.training."):
            from biomerte.motion_training import TrainingStore
            try:
                return TrainingStore(self.database).query(contract.split('.')[-1].split(':')[0],inputs)
            except (ValueError,TypeError) as error:
                raise QueryError("invalid_request",str(error)) from error
            except sqlite3.Error as error:
                raise QueryError("training_store_unavailable","Training store is unavailable") from error
        allowed={"include_parameters"} if contract=="robot.motion.model:1" else ({"target","initial_state","constraints","model_revision"} if contract=="robot.motion.plan:1" else {"pulses","initial_state","step_s","model_revision"})
        if set(inputs)-allowed:raise QueryError("invalid_request","Unknown motion input fields")
        saved,freshness,digest=self.read()
        from biomerte.motion_model import MotionModel
        model=MotionModel(saved)
        model.state.update(copy.deepcopy(saved.get("state",{})))
        if not freshness["live_state_available"]:
            model.state.update(steering_known=False,speed_m_s=None,yaw_rate_deg_s=None)
        if contract=="robot.motion.model:1":
            if "include_parameters" in inputs and type(inputs["include_parameters"]) is not bool:
                raise QueryError("invalid_request","include_parameters must be Boolean")
            result=model.status()
            result["observations"]=model.angular.count
            if inputs.get("include_parameters"):result["parameters"]=saved
        else:
            revision=inputs.get("model_revision")
            if revision is not None and (type(revision) is not int or revision!=model.revision):
                raise QueryError("model_revision_conflict","Requested revision is not the current model snapshot")
            try:
                if contract=="robot.motion.plan:1":
                    from biomerte.motion_planning import plan_motion
                    if "target" not in inputs:raise ValueError("target is required")
                    result=plan_motion(model,inputs["target"],inputs.get("initial_state"),inputs.get("constraints"))
                else:
                    if "pulses" not in inputs:raise ValueError("pulses is required")
                    result=model.predict(inputs["pulses"],inputs.get("initial_state"),inputs.get("step_s",.05))
            except (ValueError,TypeError,OverflowError) as error:
                raise QueryError("invalid_request",str(error)) from error
        result.update(freshness=freshness,model_snapshot_sha256=digest,executes_motion=False,
            qualification="observational_model_physical_prediction_accuracy_unqualified")
        return result

class Remote:
    realm="site"
    def __init__(self,cfg):
        self.node,self.site=cfg["provider"],cfg["site"]
        directory=Path(cfg["gateway_state"])
        sys.path.insert(0,str(directory))
        spec=importlib.util.spec_from_file_location("motion_gateway",directory/"mac_provider.py")
        self.gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.gateway)
    def call(self,name,args,rid=None):
        # Placement is reported to the gateway, which selects the authority.
        return self.gateway.invoke_provider(name,args,rid,self.realm,self.node)

def local_offers(cfg):
    return [dict(provider=cfg["provider"],contract=contract,resource=cfg["resource"],
        service="robot-motion",capacity=cfg["slots"],state="ready",
        usage=dict(before="Use this generic contract with cap.acquire/invoke/status or cap.submit. Provider selection is automatic; predictions never command motion.",
            read_result="Read cap.status.result.result. Inspect freshness, support, retained steering and uncertainty. A successful read is not physical calibration."),
        argv=[cfg["python"],cfg["adapter_path"],"execute","--state",cfg["state"],"--contract",contract])
        for contract in CONTRACTS]

def register_runner_contracts():
    # The current shared runner is generic, but its built-in allowlist contains
    # terminal/Docker. Opt this provider's declared read handlers into the same
    # durable execution/custody/result protocol in this process.
    from biomerte import supervised_commands
    supervised_commands.CONTRACTS=tuple(dict.fromkeys(
        tuple(supervised_commands.CONTRACTS)+tuple(CONTRACTS)))
    return supervised_commands.CONTRACTS

def serve(cfg):
    sys.path.insert(0,cfg.get("worker_runtime",cfg["runtime"]))
    from biomerte.worker import Worker,ActionPool
    register_runner_contracts()
    state=Path(cfg["state"]);state.mkdir(parents=True,exist_ok=True)
    stopped=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stopped.set())
    with (state/"worker.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        remote=Remote(cfg)
        offers=local_offers(cfg)
        worker=Worker(str(state/"worker.sqlite"),remote,offers,None)
        pool=ActionPool(worker,cfg["slots"])
        def publish():
            previous=None
            while not stopped.is_set():
                try:
                    ModelBackend(cfg["database"]).read()
                    status="ready"
                except (QueryError,ValueError,OSError):status="offline"
                try:
                    if status!=previous:
                        for contract,spec in CONTRACTS.items():
                            remote.call("cap.define",spec)
                            args={k:v for k,v in next(o for o in offers if o["contract"]==contract).items() if k!="argv"}
                            args["state"]=status
                            args["configuration"]={"execution_custody":True}
                            remote.call("cap.offer",args)
                        previous=status
                except Exception as error:
                    print("Motion registration retry:",type(error).__name__,file=sys.stderr,flush=True)
                stopped.wait(2.)
        publisher=threading.Thread(target=publish,name="motion-registration",daemon=True);publisher.start()
        try:
            while not stopped.is_set():
                try:worker.once(pool)
                except Exception as error:
                    print("Motion provider retry:",type(error).__name__,file=sys.stderr,flush=True)
                stopped.wait(.1)
        finally:
            stopped.set()
            pool.executor.shutdown(wait=True)
            for offer in offers:
                try:remote.call("cap.offer",dict(provider=cfg["provider"],contract=offer["contract"],resource=cfg["resource"],service="robot-motion",capacity=cfg["slots"],state="offline"))
                except Exception:pass
            worker.db.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["serve","execute"])
    parser.add_argument("--state",required=True)
    parser.add_argument("--contract",choices=list(CONTRACTS))
    args=parser.parse_args()
    cfg=json.loads((Path(args.state)/"provider.json").read_text())
    sys.path.insert(0,cfg["runtime"])
    if args.mode=="serve":
        serve(cfg);return
    try:
        action=json.load(sys.stdin)
        result=ModelBackend(cfg["database"]).query(args.contract,action.get("inputs",{}))
    except QueryError as error:result={"error":{"code":error.code,"message":error.message},"executes_motion":False}
    except Exception as error:result={"error":{"code":"model_query_failed","message":type(error).__name__},"executes_motion":False}
    json.dump({"result":result},sys.stdout,allow_nan=False)

if __name__=="__main__":
    os.umask(0o077)
    main()
