#!/usr/bin/env python3
"""Build exact-source images locally; export now and publish to a registry later.

Python 3.9+ build-host utility. Never provisions a registry or restarts services.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
PLATFORMS = {"linux/amd64": ("amd64", ""), "linux/arm64": ("arm64", ""),
             "linux/arm/v7": ("arm", "v7")}

def call(argv, cwd=None):
    result = subprocess.run([str(x) for x in argv], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError("{} failed ({}): {}".format(argv[0], result.returncode,
                                                       result.stderr.strip()))
    return result.stdout.strip()

def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)

def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".image-release-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def source_url(raw):
    if raw.startswith("git@") and ":" in raw:
        host, path = raw[4:].split(":", 1)
        raw = "https://" + host + "/" + path
    parsed = urlsplit(raw)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("Source origin must be an HTTP(S) or SSH Git URL")
    host = parsed.hostname + (":" + str(parsed.port) if parsed.port else "")
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))

def plan_path(source, requested):
    path = Path(requested)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Build catalog must be inside the committed source snapshot")
    resolved = (source / path).resolve()
    if not resolved.is_relative_to(source.resolve()):
        raise ValueError("Build catalog escapes the committed source snapshot")
    return resolved

def expected_labels(source_commit, source_url, image, platform):
    return {"org.opencontainers.image.revision": source_commit,
            "org.opencontainers.image.source": source_url,
            "io.biomerte.image": image, "io.biomerte.platform": platform}

def check_provenance(inspected, expected):
    labels = inspected.get("Config", {}).get("Labels") or {}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise ValueError("Local image provenance does not match the release lock")

def load_plan(path):
    plan = read_json(path)
    if plan.get("schema") != "biomerte-container-builds/1":
        raise ValueError("Unsupported image build catalog")
    images = {}
    for item in plan.get("images", []):
        name = item.get("id", "")
        dockerfile = Path(item.get("dockerfile", ""))
        if not ID.fullmatch(name) or name in images:
            raise ValueError("Image IDs must be unique safe names")
        if dockerfile.is_absolute() or ".." in dockerfile.parts or not dockerfile.parts:
            raise ValueError("Dockerfile must be inside the source snapshot")
        if not item.get("platforms") or any(p not in PLATFORMS for p in item["platforms"]):
            raise ValueError("Image has unsupported or missing platforms")
        if item.get("base_argument") not in (None, "PYTHON_IMAGE", "NODE_IMAGE"):
            raise ValueError("Only public base-image build arguments are accepted")
        images[name] = item
    if not images:
        raise ValueError("No image targets")
    return images

def inspect_image(docker, image, platform):
    data = json.loads(call([docker, "image", "inspect", image]))[0]
    if not DIGEST.fullmatch(data.get("Id", "")):
        raise ValueError("Docker did not return an immutable image ID")
    arch, variant = PLATFORMS[platform]
    if data.get("Os") != "linux" or data.get("Architecture") != arch:
        raise ValueError("Image architecture does not match " + platform)
    if variant and data.get("Variant") and data["Variant"] != variant:
        raise ValueError("Image variant does not match " + platform)
    return data

def archive_source(repo, revision, destination):
    archive = destination / "source.tar"
    call(["git", "archive", "--format=tar", "--output", archive, revision], cwd=repo)
    source = destination / "source"
    source.mkdir()
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe source archive path")
            if member.issym() or member.islnk():
                target = (source / path.parent / member.linkname).resolve()
                if not target.is_relative_to(source.resolve()):
                    raise ValueError("Source link escapes the snapshot")
            elif not (member.isfile() or member.isdir()):
                raise ValueError("Unsupported source archive member")
        handle.extractall(source)
    return source

def resolve_base(docker, image, platform):
    try:
        data = inspect_image(docker, image, platform)
    except (RuntimeError, ValueError, IndexError, KeyError):
        call([docker, "pull", "--platform", platform, image])
        data = inspect_image(docker, image, platform)
    digests = data.get("RepoDigests") or []
    if not digests:
        # An archive-loaded, correctly tagged base can match the architecture
        # while having no registry receipt. Pull it before assigning provenance.
        call([docker, "pull", "--platform", platform, image])
        data = inspect_image(docker, image, platform)
        digests = data.get("RepoDigests") or []
    if not digests or not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", digests[0]):
        raise ValueError("Base image has no immutable registry digest: " + image)
    return digests[0]

def engine_platform(docker):
    info = json.loads(call([docker, "info", "--format", "{{json .}}"]))
    architecture = info.get("Architecture", "")
    aliases = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64",
               "amd64": "amd64", "armv7l": "arm/v7", "arm": "arm/v7"}
    platform = info.get("OSType", "") + "/" + aliases.get(architecture, architecture)
    if platform not in PLATFORMS:
        raise ValueError("Unsupported Docker Engine platform: " + platform)
    return platform

def build(args):
    repo = Path(args.repo).resolve()
    revision = call(["git", "rev-parse", args.revision + "^{commit}"], cwd=repo)
    if not COMMIT.fullmatch(revision):
        raise ValueError("Expected a full source commit")
    origin = source_url(call(["git", "remote", "get-url", "origin"], cwd=repo))
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / "images.lock.json"
    if lock_path.exists():
        raise ValueError("Output already has a release; use a distinct output directory")
    lock = {"schema": "biomerte-container-release/1", "source_commit": revision,
            "source_url": origin, "created_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(), "complete": False, "images": []}
    with tempfile.TemporaryDirectory(prefix="biomerte-image-source-") as temp:
        source = archive_source(repo, revision, Path(temp))
        plan = load_plan(plan_path(source, args.plan))
        selected = list(dict.fromkeys(args.image or list(plan)))
        if any(name not in plan for name in selected):
            raise ValueError("Unknown image target")
        requested = list(dict.fromkeys(args.platform or [engine_platform(args.docker)]))
        if any(platform not in PLATFORMS for platform in requested):
            raise ValueError("Unsupported requested platform")
        save_json(lock_path, lock)
        for name in selected:
            spec = plan[name]
            for platform in requested:
                if platform not in spec["platforms"]:
                    raise ValueError(name + " does not support " + platform)
                suffix = platform.replace("/", "-")
                tag = "biomerte-local/{}:{}-{}".format(name, revision[:12], suffix)
                iidfile = Path(temp) / (name + "-" + suffix + ".iid")
                dockerfile = source / spec["dockerfile"]
                argv = [args.docker, "build", "--platform", platform,
                        "--iidfile", iidfile, "--tag", tag,
                        "--label", "org.opencontainers.image.source=" + origin,
                        "--label", "org.opencontainers.image.revision=" + revision,
                        "--label", "io.biomerte.image=" + name,
                        "--label", "io.biomerte.platform=" + platform, "--file", dockerfile]
                base = None
                if spec.get("base_image"):
                    if "ARG " + spec["base_argument"] not in dockerfile.read_text():
                        raise ValueError("Dockerfile does not expose its base-image argument")
                    base = resolve_base(args.docker, spec["base_image"], platform)
                    argv += ["--build-arg", spec["base_argument"] + "=" + base]
                argv.append(source)
                call(argv)
                image = iidfile.read_text().strip()
                data = inspect_image(args.docker, image, platform)
                check_provenance(data, expected_labels(revision, origin, name, platform))
                if spec.get("smoke"):
                    call([args.docker, "run", "--rm", "--network", "none", image] + spec["smoke"])
                artifact = output / (name + "-" + suffix + ".tar")
                call([args.docker, "save", "--output", artifact, image])
                lock["images"].append({"id": name, "platform": platform, "image_id": image,
                    "tag": tag, "dockerfile": spec["dockerfile"], "base_image": base,
                    "archive": {"file": artifact.name, "sha256": checksum(artifact),
                                "bytes": artifact.stat().st_size}, "registry_ref": None})
                save_json(lock_path, lock)
                print(json.dumps({"built": name, "platform": platform, "image_id": image,
                                  "archive": str(artifact)}), flush=True)
    lock["complete"] = True
    save_json(lock_path, lock)
    return {"lock": str(lock_path), "source_commit": revision, "images": len(lock["images"])}

def release(path, require_complete=True):
    data = read_json(path)
    if data.get("schema") != "biomerte-container-release/1":
        raise ValueError("Unsupported release lock")
    if require_complete and data.get("complete") is not True:
        raise ValueError("Release build is incomplete")
    if not COMMIT.fullmatch(data.get("source_commit", "")):
        raise ValueError("Invalid release source commit")
    seen = set()
    for item in data.get("images", []):
        key = (item.get("id"), item.get("platform"))
        if key in seen or not ID.fullmatch(key[0] or "") or key[1] not in PLATFORMS:
            raise ValueError("Invalid or duplicate image target")
        seen.add(key)
        if not DIGEST.fullmatch(item.get("image_id", "")):
            raise ValueError("Image reference is not immutable")
        registry_ref = item.get("registry_ref")
        if registry_ref is not None and not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", registry_ref):
            raise ValueError("Registry reference is not immutable")
        artifact = item.get("archive", {})
        name = artifact.get("file", "")
        if Path(name).name != name or name in ("", ".", ".."):
            raise ValueError("Archive must be a sibling of the release lock")
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")):
            raise ValueError("Missing archive checksum")
    if not seen:
        raise ValueError("Release has no images")
    return data

def verify(args):
    data = release(args.lock)
    folder = Path(args.lock).resolve().parent
    for item in data["images"]:
        path = folder / item["archive"]["file"]
        if path.stat().st_size != item["archive"]["bytes"] or checksum(path) != item["archive"]["sha256"]:
            raise ValueError("Archive integrity failed: " + path.name)
    return {"verified": True, "source_commit": data["source_commit"], "images": len(data["images"])}

def registry_prefix(registry, namespace):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*(?::[0-9]+)?", registry or ""):
        raise ValueError("Supply an actual registry hostname, without credentials or URL scheme")
    if registry != "localhost" and "." not in registry and ":" not in registry:
        raise ValueError("Registry must be localhost, a dotted hostname, or a host with an explicit port")
    parts = (namespace or "").split("/")
    if not parts or any(not ID.fullmatch(p) or p in (".", "..") for p in parts):
        raise ValueError("Supply a valid existing registry namespace")
    return registry + "/" + namespace

def publish(args):
    data = release(args.lock)
    prefix = registry_prefix(args.registry, args.namespace)
    # Check every target before any tag or push, so a bad later entry cannot
    # cause a partially published release under a different source or target.
    for item in data["images"]:
        inspected = inspect_image(args.docker, item["image_id"], item["platform"])
        check_provenance(inspected, expected_labels(
            data["source_commit"], data["source_url"], item["id"], item["platform"]))
    for item in data["images"]:
        tag = "{}/{}:{}-{}".format(prefix, item["id"], data["source_commit"][:12],
                                   item["platform"].replace("/", "-"))
        call([args.docker, "tag", item["image_id"], tag])
        output = call([args.docker, "push", tag])
        matches = re.findall(r"digest:\s*(sha256:[0-9a-f]{64})", output)
        if not matches:
            raise ValueError("Push returned no confirmed registry digest; inspect before retry")
        item["registry_ref"] = tag.rsplit(":", 1)[0] + "@" + matches[-1]
        save_json(args.lock, data)
        print(json.dumps({"published": item["id"], "platform": item["platform"],
                          "registry_ref": item["registry_ref"]}), flush=True)
    return {"lock": str(Path(args.lock).resolve()), "published": len(data["images"])}

def render(args):
    data = release(args.lock)
    selected = [x for x in data["images"] if x["id"] == args.image]
    if not selected:
        raise ValueError("Release does not contain the selected image")
    refs = {}
    for item in selected:
        ref = item["image_id"] if args.local else item.get("registry_ref")
        if not ref:
            raise ValueError("Image is not published; --local requires preloaded images")
        refs[item["platform"]] = ref
    spec = read_json(args.template)
    spec["images"] = refs
    sys.path.insert(0, str(ROOT))
    from biomerte.docker_services import revision
    spec["revision"] = revision(spec)
    save_json(args.output, spec)
    return {"manifest": str(Path(args.output).resolve()), "revision": spec["revision"],
            "platforms": sorted(refs), "local_images_require_preload": args.local}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("build")
    p.add_argument("--repo", default=str(ROOT))
    p.add_argument("--revision", default="HEAD")
    p.add_argument("--plan", default="deploy/docker-services/images.json")
    p.add_argument("--image", action="append")
    p.add_argument("--platform", action="append")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--docker", default="docker")
    p.set_defaults(func=build)
    p = commands.add_parser("verify")
    p.add_argument("--lock", required=True)
    p.set_defaults(func=verify)
    p = commands.add_parser("publish")
    p.add_argument("--lock", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--docker", default="docker")
    p.set_defaults(func=publish)
    p = commands.add_parser("render")
    p.add_argument("--lock", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--local", action="store_true")
    p.set_defaults(func=render)
    args = parser.parse_args()
    try:
        print(json.dumps(args.func(args), sort_keys=True))
    except (RuntimeError, ValueError, OSError, KeyError, IndexError) as error:
        parser.exit(1, "Image release failed: " + str(error) + "\n")

if __name__ == "__main__":
    main()
