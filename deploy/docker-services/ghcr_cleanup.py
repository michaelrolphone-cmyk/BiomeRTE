#!/usr/bin/env python3
"""One-time, exact-inventory cleanup of publication 44648102932. Verify by default."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

OWNER = "michaelrolphone-cmyk"
OWNER_ID = 247690798
REPOSITORY = OWNER + "/BiomeRTE"
REPOSITORY_ID = 1443999162
BRANCH = "refs/heads/work/ghcr-guarded-cleanup-20261120"
RUN_ID = 44648102932
SOURCE = "a81497cf0465e980585cf72562c5bef059b3c7b7"
INVENTORY_SHA256 = "d2190b7366355208135cde9f48fb2b0cfe3fb22a9970aab735d9448a3f697e00"
PACKAGE_IDS = {"service-runner": 17087076, "service-authority": 17087080, "portable-services": 17087092}
VERSION_IDS = {1601148152, 1601148605, 1601148967, 1601149362, 1601149827, 1601150350}
EXPECTED_DIGESTS = {'service-runner': {'sha256:92d1d9803249b55ad2588b88291d033b3763a648de1ceeda095e394651ce6c05': ['a81497cf0465-linux-arm64'], 'sha256:54a942a9f620952f4b41a54ce4054d2f180de2ba1886792489f99269697d6d38': ['a81497cf0465-linux-arm-v7']}, 'service-authority': {'sha256:e66fac75207dad0a133ac31300b21bcd8a8721ac2def953f3b25b2cce42d7292': ['a81497cf0465-linux-arm64'], 'sha256:bf64fcfa88b926997e307405c20cd6d5775e4c031c893bc322bdf3cabe9f7d28': ['a81497cf0465-linux-arm-v7']}, 'portable-services': {'sha256:f356ab913689f96a8876506f567f01576ee78b57571384d79e9e10298664acae': ['a81497cf0465-linux-arm64'], 'sha256:1403d559ccc756dfb7d4add01c36a63e7d4a85a04fb46c3eddab23e581cdf0d2': ['a81497cf0465-linux-arm-v7']}}
EARLIEST = "2026-11-20T00:49:44Z"
LATEST = "2026-11-20T00:50:17Z"
INVENTORY = Path(__file__).with_name("ghcr-cleanup-44648102932.inventory.json")
PRIOR_RUN_ID = 44654682862
PRIOR_WORKFLOW_SHA = "dd72877d2a33dac489e519ac0af563e75f5b2e9c"
PRIOR_RECEIPT_SHA256 = "383c1acf36568d0281e7ae9258d666c8d7c5c5497311628256971c462e74dd25"
PRIOR_RECEIPT = Path(__file__).with_name("ghcr-cleanup-44654682862.prior-receipt.json")
REMOVED_VERSION_ID = 1601148605
ACCEPT_MANIFEST = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
))

class Closed(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise Closed(message)

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def timestamp(value):
    require(isinstance(value, str), "Missing creation timestamp")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Closed("Invalid creation timestamp") from None
    require(parsed.tzinfo is not None, "Creation timestamp lacks a timezone")
    return parsed

def package_metadata(value):
    owner = value.get("owner") or {}
    repo = value.get("repository") or {}
    return {
        "id": value.get("id"), "name": value.get("name"),
        "package_type": value.get("package_type"), "visibility": value.get("visibility"),
        "owner_id": owner.get("id"), "owner_login": owner.get("login"),
        "repository_id": repo.get("id"), "repository": repo.get("full_name"),
        "created_at": value.get("created_at"),
    }

def version_metadata(value):
    return {
        "id": value.get("id"), "digest": value.get("name"),
        "tags": (value.get("metadata") or {}).get("container", {}).get("tags"),
        "created_at": value.get("created_at"), "updated_at": value.get("updated_at"),
    }

def load_expected(path=INVENTORY):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == INVENTORY_SHA256, "Inventory file hash mismatch")
    value = json.loads(raw)
    require(value.get("schema") == "biomerte-ghcr-publication-inventory/1"
            and value.get("read_only") is True and value.get("verified") is True
            and value.get("original_run_id") == RUN_ID
            and value.get("original_source_commit") == SOURCE
            and value.get("repository") == REPOSITORY, "Unexpected original publication inventory")
    entries = value.get("packages", [])
    require(len(entries) == 3, "Expected exactly three packages")
    expected = {}
    all_ids = set()
    for entry in entries:
        name = entry.get("requested_name")
        require(name in PACKAGE_IDS and name not in expected, "Unexpected or duplicate package")
        package = entry["package"]
        meta = package_metadata(package)
        require(entry.get("identity_verified") is True
                and entry.get("inventory_complete") is True
                and entry.get("matches_original_publication") is True, "Inventory was not fully verified")
        require(meta["id"] == entry.get("expected_package_id") == PACKAGE_IDS[name]
                and meta["name"] == name and meta["package_type"] == "container"
                and meta["visibility"] == "public"
                and meta["owner_id"] == OWNER_ID and meta["owner_login"] == OWNER
                and meta["repository_id"] == REPOSITORY_ID and meta["repository"] == REPOSITORY,
                "Original package identity mismatch")
        versions = entry["versions"]
        require(package.get("version_count") == len(versions) == 2, "Expected two versions per package")
        require({v["digest"]: v["tags"] for v in versions} == EXPECTED_DIGESTS[name],
                "Original digest or tag inventory mismatch")
        for version in versions:
            vid = version.get("id")
            require(type(vid) is int and vid in VERSION_IDS and vid not in all_ids,
                    "Unexpected or duplicate original version ID")
            all_ids.add(vid)
            require(timestamp(EARLIEST) <= timestamp(version["created_at"]) <= timestamp(LATEST),
                    "Version creation falls outside the observed publication window")
            require(version["updated_at"] == version["created_at"],
                    "Original version changed after creation")
        require(package["created_at"] == min(v["created_at"] for v in versions),
                "Package was not created by this publication")
        expected[name] = {"metadata": meta, "versions": versions}
    require(set(expected) == set(PACKAGE_IDS) and all_ids == VERSION_IDS,
            "Incomplete original publication allowlist")
    return expected

def validate_resume_proof(prior, original):
    require(prior.get("schema") == "biomerte-ghcr-cleanup-receipt/1"
            and prior.get("original_run_id") == RUN_ID and prior.get("source_commit") == SOURCE
            and prior.get("inventory_sha256") == INVENTORY_SHA256
            and prior.get("workflow_sha") == PRIOR_WORKFLOW_SHA
            and prior.get("mode") == "delete" and prior.get("complete") is False
            and prior.get("state") == "failed_requires_inspection"
            and prior.get("error") == "Exact version deletion did not return HTTP 204",
            "Unexpected prior cleanup outcome")
    require(prior.get("before") == original and prior.get("anonymous_checks") == [],
            "Prior cleanup did not establish the complete original inventory")
    originals = {version["id"]: version for version in original["service-runner"]["versions"]}
    actions = prior.get("actions", [])
    require(len(actions) == 2, "Prior cleanup has unexpected additional attempts")
    expected = [(REMOVED_VERSION_ID, 204), (1601148152, 400)]
    for action, (vid, status) in zip(actions, expected):
        require(action.get("operation") == "delete_version"
                and action.get("package") == "service-runner"
                and action.get("package_id") == PACKAGE_IDS["service-runner"]
                and action.get("version_id") == vid and action.get("http_status") == status
                and action.get("digest") == originals[vid]["digest"],
                "Prior cleanup does not prove the exact single removed version")
    require([action["version_id"] for action in actions if action["http_status"] == 204]
            == [REMOVED_VERSION_ID], "Unexpected confirmed removal history")

def load_resume_expected(original, path=PRIOR_RECEIPT):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == PRIOR_RECEIPT_SHA256, "Prior cleanup receipt hash mismatch")
    validate_resume_proof(json.loads(raw), original)
    remaining = {
        name: {"metadata": package["metadata"],
               "versions": [version for version in package["versions"]
                            if version["id"] != REMOVED_VERSION_ID]}
        for name, package in original.items()
    }
    require({name: len(package["versions"]) for name, package in remaining.items()}
            == {"service-runner": 1, "service-authority": 2, "portable-services": 2},
            "Unexpected remaining version inventory")
    return remaining

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None

OPENER = urllib.request.build_opener(NoRedirect())

def http(url, method="GET", headers=None, read_json=True):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with OPENER.open(request, timeout=30) as response:
            status = response.status
            links = response.headers.get("Link", "")
            value = json.load(response) if status == 200 and read_json else None
            return status, value, links
    except urllib.error.HTTPError as error:
        return error.code, None, ""
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise Closed("Request did not return a verified response; outcome requires inspection") from None

class GitHub:
    def __init__(self, token, expected, allow_delete=False):
        require(isinstance(token, str) and bool(token), "Workflow token missing")
        self.token = token
        self.allow_delete = allow_delete
        self.allowed = set()
        self.deletable = set()
        for name, package in expected.items():
            path = self.path(name)
            self.allowed.update((path, path + "/versions"))
            self.deletable.add(path)

    @staticmethod
    def path(name):
        require(name in PACKAGE_IDS, "Package outside the cleanup allowlist")
        return "/users/" + OWNER + "/packages/container/" + name

    def request(self, path, method="GET"):
        parsed = urllib.parse.urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment
                and parsed.path in self.allowed, "API path outside the cleanup allowlist")
        require(method == "GET" or (method == "DELETE" and self.allow_delete
                and parsed.path in self.deletable and not parsed.query), "API mutation is not allowed")
        return http("https://api.github.com" + path, method, {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "biomerte-exact-publication-cleanup",
        })

    def package(self, name):
        status, value, _ = self.request(self.path(name))
        if status == 404:
            return None
        require(status == 200 and isinstance(value, dict), "Package read failed: HTTP " + str(status))
        return value

    def versions(self, name):
        base = self.path(name) + "/versions"
        path = base
        seen = set()
        result = []
        while path:
            require(path not in seen and len(seen) < 100, "Incomplete or cyclic version pagination")
            seen.add(path)
            status, page, links = self.request(path)
            require(status == 200 and isinstance(page, list), "Version inventory read failed")
            result.extend(page)
            path = None
            for link in links.split(",") if links else []:
                match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="([^"]+)"\s*', link)
                require(match is not None, "Unrecognized pagination metadata")
                if match.group(2) == "next":
                    parsed = urllib.parse.urlsplit(match.group(1))
                    require(path is None and parsed.scheme == "https" and parsed.netloc == "api.github.com"
                            and parsed.path == base and not parsed.fragment, "Unsafe pagination destination")
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
        ids = [v.get("id") for v in result]
        require(all(type(vid) is int and vid > 0 for vid in ids)
                and len(ids) == len(set(ids)), "Invalid or duplicate version inventory")
        return result

    def delete_package(self, name):
        return self.request(self.path(name), "DELETE")[0]

    def anonymous(self, name, reference):
        require(name in PACKAGE_IDS, "Anonymous check outside package allowlist")
        scope = "repository:" + OWNER + "/" + name + ":pull"
        url = "https://ghcr.io/token?" + urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
        status, value, _ = http(url, headers={"Cache-Control": "no-cache"})
        result = {"package": name, "reference": reference, "token_status": status}
        if status in (401, 403):
            result["denied"] = True
            result["reason"] = "fresh_anonymous_pull_authorization_denied"
            return result
        require(status == 200 and isinstance(value, dict), "Anonymous token check was inconclusive")
        token = value.get("token") or value.get("access_token")
        require(isinstance(token, str) and bool(token), "Anonymous token was missing")
        manifest = "https://ghcr.io/v2/" + OWNER + "/" + name + "/manifests/" + urllib.parse.quote(reference, safe=":")
        status, _, _ = http(manifest, headers={
            "Authorization": "Bearer " + token, "Accept": ACCEPT_MANIFEST, "Cache-Control": "no-cache",
        }, read_json=False)
        result.update({"manifest_status": status, "denied": status in (401, 403, 404)})
        require(status in (200, 401, 403, 404), "Anonymous manifest check was inconclusive")
        return result

def inspect_current(api, name, expected, remaining, allow_absent=False):
    package = api.package(name)
    if package is None:
        require(allow_absent and not remaining, "Expected package is unexpectedly absent")
        return None
    require(package_metadata(package) == expected["metadata"], "Package identity or creation metadata changed: " + name)
    versions = api.versions(name)
    normalized = [version_metadata(v) for v in versions]
    require(package.get("version_count") == len(normalized) == len(remaining),
            "Package version count changed: " + name)
    require(sorted(normalized, key=lambda v: v["id"]) == sorted(remaining, key=lambda v: v["id"]),
            "Version IDs, digests, tags or timestamps changed: " + name)
    return {"metadata": package_metadata(package), "versions": normalized}

def perform(api, remaining, original, execute, receipt, save):
    receipt["before"] = {}
    for name, package in remaining.items():
        receipt["before"][name] = inspect_current(api, name, package, package["versions"])
    receipt["state"] = "resume_preflight_verified"
    save()
    if not execute:
        receipt.update({"state": "verified_only", "complete": True})
        save()
        return
    for name, package in remaining.items():
        observed = inspect_current(api, name, package, package["versions"])
        action = {
            "operation": "delete_exact_original_package", "package": name,
            "package_id": PACKAGE_IDS[name], "started_at": now(),
            "remaining_version_ids": [version["id"] for version in package["versions"]],
            "verified_immediately_before": observed,
        }
        receipt["actions"].append(action)
        save()
        action["http_status"] = api.delete_package(name)
        action["responded_at"] = now()
        save()
        require(action["http_status"] == 204, "Exact package deletion did not return HTTP 204")
        action["package_absent_after"] = api.package(name) is None
        save()
        require(action["package_absent_after"], "Package still exists after cleanup: " + name)
    for name, package in original.items():
        require(api.package(name) is None, "Package reappeared during cleanup: " + name)
        for version in package["versions"]:
            for reference in [version["digest"]] + version["tags"]:
                result = api.anonymous(name, reference)
                receipt["anonymous_checks"].append(result)
                save()
                require(result.get("denied") is True, "Anonymous registry access remains available")
    receipt.update({"state": "deleted_and_anonymous_access_denied", "complete": True})
    save()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("verify", "delete"), default="verify")
    parser.add_argument("--output", default="ghcr-cleanup-44648102932.receipt.json")
    args = parser.parse_args()
    receipt = {"schema": "biomerte-ghcr-cleanup-receipt/1", "original_run_id": RUN_ID,
               "source_commit": SOURCE, "inventory_sha256": INVENTORY_SHA256,
               "observed_creation_window": {"start": EARLIEST, "end": LATEST},
               "workflow_sha": os.environ.get("GITHUB_SHA"), "mode": args.mode,
               "started_at": now(), "complete": False, "state": "starting",
               "actions": [], "anonymous_checks": [],
               "resume": {"prior_run_id": PRIOR_RUN_ID, "prior_workflow_sha": PRIOR_WORKFLOW_SHA,
                          "prior_receipt_sha256": PRIOR_RECEIPT_SHA256,
                          "confirmed_removed_version_ids": [REMOVED_VERSION_ID],
                          "method": "exact_original_package_delete"}}
    def save():
        receipt["updated_at"] = now()
        Path(args.output).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    save()
    try:
        require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
                and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == BRANCH, "Unexpected workflow execution context")
        original = load_expected()
        remaining = load_resume_expected(original)
        api = GitHub(os.environ.get("GH_TOKEN"), remaining, allow_delete=args.mode == "delete")
        perform(api, remaining, original, args.mode == "delete", receipt, save)
    except Exception as error:
        receipt["state"] = "failed_requires_inspection"
        receipt["error"] = str(error) if isinstance(error, Closed) else "Unexpected validation or I/O failure"
        save()
        print(json.dumps({"complete": False, "error": receipt["error"]}), flush=True)
        return 1
    print(json.dumps({"complete": receipt["complete"], "state": receipt["state"],
                      "actions": len(receipt["actions"]), "receipt": args.output}), flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
