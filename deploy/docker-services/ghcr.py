#!/usr/bin/env python3
"""GitHub qualification and private-visibility checks; never publishes images."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

COMMIT = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

def api(path, missing=False):
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise ValueError("GH_TOKEN is required for GitHub authorization checks")
    request = Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "biomerte-image-release"})
    try:
        with urlopen(request) as response:
            return json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise ValueError("GitHub authorization check failed with HTTP " + str(error.code)) from None
    except (URLError, json.JSONDecodeError):
        raise ValueError("GitHub authorization check did not return verified metadata") from None

def publishing_repository(repository, owner):
    actual = os.environ.get("GITHUB_REPOSITORY")
    if actual:
        if repository and repository.lower() != actual.lower():
            raise ValueError("Publishing repository does not match the workflow repository")
        repository = actual
    if (not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository)
            or repository.split("/", 1)[0].lower() != owner.lower()):
        raise ValueError("A verified publishing repository is required before creating a private package")
    value = api("/repos/" + repository)
    if (value.get("full_name", "").lower() != repository.lower() or
            value.get("private") is not True or value.get("visibility") != "private"):
        raise ValueError("Refusing private package creation from publishing repository {} "
                         "(visibility={}, private={})".format(
                             repository, json.dumps(value.get("visibility")),
                             json.dumps(value.get("private"))))
    return {"name": value["full_name"], "visibility": "private", "private": True}

def private_packages(owner, images, require_existing=False, repository=None):
    if not OWNER.fullmatch(owner) or not images or any(not NAME.fullmatch(x) for x in images):
        raise ValueError("Expected a GitHub owner and explicit image IDs")
    account = api("/users/" + quote(owner, safe=""))
    kind = {"User": "users", "Organization": "orgs"}.get(account.get("type"))
    if not kind or account.get("login", "").lower() != owner.lower():
        raise ValueError("GitHub did not confirm the registry owner")
    packages = []
    creation_repository = None
    for name in sorted(set(images)):
        package = api("/{}/{}/packages/container/{}".format(
            kind, quote(owner, safe=""), quote(name, safe="")), missing=not require_existing)
        if package is None:
            if require_existing:
                raise ValueError("Required private package was not found: " + name)
            # GITHUB_TOKEN creation inherits the publishing repository's
            # visibility. The generic Container registry private default
            # does not establish privacy for a workflow in a public repo.
            if creation_repository is None:
                creation_repository = publishing_repository(repository, owner)
            packages.append({"name": name, "state": "absent",
                             "new_package_visibility": "private",
                             "creation_repository": creation_repository})
            continue
        if package.get("visibility") != "private":
            raise ValueError("Refusing a package without verified private visibility: {} "
                             "(visibility={})".format(name, json.dumps(package.get("visibility"))))
        if package.get("name") != name or package.get("package_type") != "container":
            raise ValueError("GitHub returned mismatched package metadata: " + name)
        packages.append({"name": name, "state": "existing", "visibility": "private",
                         "id": package.get("id"), "url": package.get("html_url")})
    return {"schema": "biomerte-ghcr-visibility/1", "owner": owner.lower(),
            "require_existing": require_existing, "packages": packages}

def qualified(repository, workflow, revision):
    parts = repository.split("/")
    if (len(parts) != 2 or not OWNER.fullmatch(parts[0]) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[1]) or
            not COMMIT.fullmatch(revision) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow)):
        raise ValueError("Expected an exact source commit, repository and workflow filename")
    path = "/repos/{}/actions/workflows/{}/runs?{}".format(repository, quote(workflow, safe=""),
        urlencode({"head_sha": revision, "branch": "main", "per_page": 100}))
    response = api(path)
    candidates = [run for run in response.get("workflow_runs", [])
        if run.get("head_sha") == revision and run.get("head_branch") == "main"
        and run.get("event") in ("push", "workflow_dispatch")
        and (run.get("head_repository") or {}).get("full_name", "").lower() == repository.lower()]
    if not candidates:
        raise ValueError("No qualification run exists for this exact main commit")
    latest = max(candidates, key=lambda run: (run["id"], run.get("run_attempt", 1)))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("The latest qualification of this exact main commit has not succeeded")
    return {"schema": "biomerte-main-qualification/1", "repository": repository,
            "source_commit": revision, "workflow": workflow, "run_id": latest["id"],
            "run_attempt": latest.get("run_attempt", 1), "url": latest.get("html_url"),
            "conclusion": "success"}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    private = sub.add_parser("private")
    private.add_argument("--owner", required=True)
    private.add_argument("--repository", help="Publishing repository; defaults to GITHUB_REPOSITORY")
    private.add_argument("--image", action="append", required=True)
    private.add_argument("--require-existing", action="store_true")
    private.add_argument("--output", required=True)
    qualify = sub.add_parser("qualified")
    qualify.add_argument("--repository", required=True)
    qualify.add_argument("--workflow", required=True)
    qualify.add_argument("--revision", required=True)
    qualify.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "private":
            result = private_packages(args.owner, args.image, args.require_existing, args.repository)
        else:
            result = qualified(args.repository, args.workflow, args.revision)
        result["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError) as error:
        parser.exit(1, "GHCR check failed: " + str(error) + "\n")

if __name__ == "__main__":
    main()
