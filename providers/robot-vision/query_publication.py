"""Recover registered native query offers in bounded, revision-checked pages."""
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid


def unsupported(error):
    return getattr(error, "status", None) in (400, 404) and str(error).strip() in {
        "Unknown operation", "Unknown operation: cap.publish", "Unknown operation: cap.registrations",
        "Invalid scoped-provider envelope"}


class QueryPublications:
    def __init__(self, remote, cfg, definitions, offer, atomic_json, diagnostic):
        self.remote, self.cfg = remote, cfg
        self.definitions, self.offer, self.save, self.diagnostic = definitions, offer, atomic_json, diagnostic
        self.scope = dict(provider=cfg["provider"], resource=cfg["resource"], service="robot-vision")
        self.directory = Path(cfg["state"]) / "provider/query-publications"
        self.legacy = False
        self.unclaimed = set()
        self.legacy_until = 0

    def pending_names(self):
        names = set()
        for path in self.directory.glob("*.json"):
            plan = json.loads(path.read_text())
            if all(plan["arguments"].get(k)==v for k,v in self.scope.items()):
                names.update(entry["offer"]["contract"] for entry in plan["arguments"]["offers"])
        return names

    def inspect(self, names):
        reply = self.remote.call("cap.registrations", dict(self.scope, contracts=list(names)))
        rows = {item["contract"]:item for item in reply["registrations"]}
        if set(rows) != set(names):
            raise RuntimeError("Incomplete registration snapshot")
        return rows

    def make_entry(self, name, state, revision):
        values = self.offer(self.cfg, name, state)
        return dict(definition=self.definitions[name],
                    offer={key:value for key,value in values.items() if key not in self.scope},
                    expected_registration=revision)

    def scalar(self, entry, registration, missing, publication):
        offer = entry["offer"]
        name = offer["contract"]
        if offer["state"] == "ready":
            self.remote.call("cap.define", entry["definition"])
        args = dict(self.scope, **offer, publication_id=publication)
        if missing:
            args["expected_absent"] = True
        else:
            args["expected_registration"] = registration or ""
        rid = "vision-scalar-" + hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        receipt = self.remote.call("cap.offer", args, rid)
        if receipt.get("contract") != name or receipt.get("state") != offer["state"] or not receipt.get("registration"):
            raise RuntimeError("Invalid conditional offer acknowledgement")
        return receipt

    def apply(self, path, plan, states, remember, stopping, snapshot=None):
        """Reconcile origin before retrying a persisted plan after a lost acknowledgement."""
        args, intent = plan["arguments"], plan["id"]
        entries = {entry["offer"]["contract"]:entry for entry in args["offers"]}
        if snapshot is None:
            snapshot = self.inspect(entries)
        changed_intent = any(states.get(name, "offline") != entry["offer"]["state"]
                             for name, entry in entries.items())
        active = []
        offline = {}
        for name, entry in entries.items():
            row = snapshot[name]
            if row.get("published_by") == intent and row.get("registration"):
                remember(name, row["state"], row["registration"])
                continue
            if row["status"] == "superseded":
                continue
            expected = entry["expected_registration"]
            if changed_intent:
                # Never replay an old ready intent after native health has changed.
                # Fence an outstanding request that could still match the original row.
                if ((row["status"] == "found" and row.get("registration") == expected)
                        or (row["status"] == "missing" and name in plan["absent"])):
                    changed = self.make_entry(name, states.get(name,"offline"), row.get("registration"))
                    v = self.scalar(changed,row.get("registration"),row["status"]=="missing",intent+"-changed")
                    remember(name,v["state"],v["registration"])
                continue
            if row["status"] == "missing" and name in plan["absent"]:
                v = self.scalar(entry,None,True,intent)
                remember(name,v["state"],v["registration"])
                continue
            if entry["offer"]["state"] == "offline":
                if row["status"] == "found":
                    if row["state"] == "offline" and (row.get("registration") == expected or
                                                      row.get("withdrawn_from") == expected):
                        remember(name,"offline",expected)
                    elif row.get("registration") == expected:
                        offline[name] = expected
                continue
            if row["status"] == "found" and row.get("registration") == expected:
                active.append(name)
        if offline:
            reply = self.remote.call("cap.withdraw",dict(self.scope,registrations=offline),intent+"-offline")
            done = set(reply["withdrawn"])
            if done | set(reply["superseded"]) != set(offline):
                raise RuntimeError("Incomplete offline registration receipt")
            for name in done:
                remember(name,"offline",offline[name])
        if not active or stopping.is_set():
            if not active:
                path.unlink()
            return
        # A retry of the same valid page uses its original signed request ID.
        published = {name:entry for name,entry in entries.items() if entry["offer"]["state"] != "offline"}
        receipt_id = plan.get("receipt_id",intent)
        try:
            result = self.remote.call("cap.publish", dict(args,offers=list(published.values())), receipt_id)
        except Exception as error:
            if (getattr(error,"status",None)==410 and
                    str(error).strip()=="Receipt result retired; request will not execute again"):
                # The write still carries its original per-row revisions and publication
                # identity. A new receipt can reconfirm rows whose old reply was retired.
                plan["receipt_id"] = "vision-retired-" + hashlib.sha256(receipt_id.encode()).hexdigest()
                self.save(path,plan)
            raise
        returned = {row["contract"]:row for row in result["offers"]}
        if set(returned) != set(published):
            raise RuntimeError("Incomplete publication acknowledgement")
        for name in active:
            row = returned[name]
            if row["status"] in ("published","unchanged") and row.get("registration"):
                remember(name,row["state"],row["registration"])
            elif row["status"] == "different":
                v = self.scalar(entries[name],entries[name]["expected_registration"],False,intent)
                remember(name,v["state"],v["registration"])
            elif row["status"] == "invalid":
                print("Vision query definition rejected:", name, file=sys.stderr, flush=True)
        path.unlink()

    def retire(self, path, plan, remember, withdraw):
        args, intent = plan["arguments"], plan["id"]
        entries = {item["offer"]["contract"]:item for item in args["offers"]}
        rows = self.inspect(entries)
        owned = {}
        for name, entry in entries.items():
            row = rows[name]
            if row["status"] == "superseded":
                continue
            if row["status"] == "missing" and name in plan["absent"]:
                # Fence an unacknowledged create-if-absent before allowing it to
                # appear after shutdown. The offline offer has no executable capacity.
                self.remote.call("cap.define",entry["definition"])
                receipt = self.scalar(self.make_entry(name,"offline",None),None,True,intent+"-stop")
                owned[name] = receipt["registration"]
            elif row["status"] == "found" and (row.get("published_by") == intent or
                                               row.get("registration") == entry["expected_registration"]):
                owned[name] = row.get("registration")
            if name in owned:
                remember(name,"offline",owned[name])
        if owned:
            withdraw(owned)
        path.unlink()

    def stop(self, remember, withdraw):
        for path in sorted(self.directory.glob("*.json")):
            try:
                plan = json.loads(path.read_text())
                if any(plan["arguments"].get(k)!=v for k,v in self.scope.items()):
                    continue
                plan["retiring"] = True
                self.save(path,plan)
                self.retire(path,plan,remember,withdraw)
            except Exception as error:
                print("Vision query publication retirement pending:",self.diagnostic(error),file=sys.stderr,flush=True)

    def reconcile(self, states, revisions, advertised, remember, stopping, withdraw):
        """Return None only when the authority explicitly lacks this protocol."""
        if self.legacy and time.monotonic() < self.legacy_until:
            return None
        self.legacy = False
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        retry = False
        busy = set()
        for path in sorted(self.directory.glob("*.json")):
            if stopping.is_set():
                return True
            plan = json.loads(path.read_text())
            if any(plan["arguments"].get(k)!=v for k,v in self.scope.items()):
                continue
            names = {entry["offer"]["contract"] for entry in plan["arguments"]["offers"]}
            try:
                if plan.get("retiring"):
                    self.retire(path,plan,remember,withdraw)
                else:
                    self.apply(path,plan,states,remember,stopping)
            except Exception as error:
                print("Vision query publication replay:", self.diagnostic(error),file=sys.stderr,flush=True)
                busy.update(names)
                retry = True
        primary = {"robot.vision.metadata:1","robot.vision.status:1"}
        names = sorted((name for name,state in states.items() if name not in busy and advertised.get(name)!=state),
                       key=lambda name:(name not in primary,name))
        for start in range(0,len(names),16):
            if stopping.is_set():
                break
            page = names[start:start+16]
            try:
                rows = self.inspect(page)
            except Exception as error:
                if unsupported(error):
                    self.legacy = True
                    self.legacy_until = time.monotonic() + 60
                    return None
                print("Vision query registration snapshot:", self.diagnostic(error), file=sys.stderr, flush=True)
                retry = True
                continue
            try:
                entries, absent = [], []
                for name in page:
                    row, desired = rows[name], states[name]
                    if row["status"] == "superseded":
                        continue
                    revision = row.get("registration")
                    ours = revisions.get(name)
                    if row["status"] == "missing":
                        if desired == "offline":
                            remember(name, desired, None)
                            continue
                        absent.append(name)
                    elif row["state"] != "offline" and (not ours or revision != ours):
                        # Leave an existing publisher in control. Its state may already
                        # satisfy demand, but its revision is not ours to reconfigure.
                        self.unclaimed.add(name)
                        if row["state"] == desired:
                            remember(name,desired,None)
                        continue
                    elif ours and revision != ours and row.get("withdrawn_from") != ours:
                        continue
                    self.unclaimed.discard(name)
                    entries.append(self.make_entry(name,desired,revision))
                # Existing offers use one conditional write. First enrollment
                # requires its own definition and create-if-absent write. Keep
                # that work independent, including after a failed definition.
                known = [entry for entry in entries if entry["offer"]["contract"] not in absent]
                groups = [known] if known else []
                groups.extend([entry] for entry in entries if entry["offer"]["contract"] in absent)
                for group in groups:
                    if stopping.is_set():
                        break
                    intent = "vision-query-" + uuid.uuid4().hex
                    args = dict(self.scope, offers=group, publication_id=intent)
                    missing = [entry["offer"]["contract"] for entry in group if entry["offer"]["contract"] in absent]
                    plan = dict(id=intent, arguments=args, absent=missing)
                    path = self.directory / (intent + ".json")
                    self.save(path, plan)
                    try:
                        self.apply(path, plan, states, remember, stopping, rows)
                    except Exception as error:
                        print("Vision query registration group:", self.diagnostic(error), file=sys.stderr, flush=True)
                        retry = True
            except Exception as error:
                print("Vision query registration page:", self.diagnostic(error),file=sys.stderr,flush=True)
                retry = True
        return retry
