#!/usr/bin/env python3
"""Prepare, verify, or install the named native Mac launcher; never start jobs.

Default CLI behavior is a side-effect-free deployment plan. --prepare compiles
and signs an isolated bundle. --install-prepared verifies that concrete stage,
requires service quiescence, and installs only its bundle/config/manifest.
install_provider.py owns the LaunchAgent files and launchd transitions.

Public integration:
    launch_agent_spec(state_dir, *, app_path=None, label=SERVICE_LABEL,
                      require_installed=False) -> dict
    validate_installed_launcher(state_dir, *, app_path=None) -> dict
    prepare_bundle(repo_root, state_dir, output_dir, *, app_path=None,
                   identity="auto", label=SERVICE_LABEL) -> dict
    install_prepared(prepared_dir, *, state_dir=None, app_path=None) -> dict

No function reads or changes Local Network privacy databases or preferences.
Local development without a valid signing certificate uses explicit ad hoc
signing. Existing ad hoc builds are reused byte-for-byte for identical inputs;
an installed ad hoc build is not silently rebuilt under a new code identity.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
BUNDLE_ID = "org.biomerte.robot-vision"
APP_NAME = "Biome Robot Vision.app"
EXECUTABLE = "BiomeRobotVision"
SERVICE_LABEL = "biome.robot-vision"
DEFAULT_STATE = Path.home() / ".local/share/biome-robot-vision"
PURPOSE = "Connects to the RC car's camera on your local network to receive video for mapping and object tracking."
AD_HOC_LIMITATION = (
    "Development ad hoc signing: Local Network permission persistence across "
    "rebuilt executable identities is not qualified."
)


def _path(value):
    return Path(value).expanduser().resolve()


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic(path, data, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_json(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Expected an owned, owner-only regular file: " + str(path))
        return json.load(stream)


def _owned_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Refusing symlink directory: " + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError("Expected an owned directory without group/other write access: " + str(path))


def default_app_path():
    return Path.home() / "Applications" / APP_NAME


def installed_paths(state_dir, app_path=None):
    state = _path(state_dir)
    return {
        "state_dir": state,
        "app_path": _path(app_path or default_app_path()),
        "config_path": state / "macos-app/launcher.json",
        "manifest_path": state / "macos-app/manifest.json",
    }


def launch_agent_spec(state_dir, *, app_path=None, label=SERVICE_LABEL, require_installed=False):
    """Pure cross-platform plan unless explicit installed verification is requested."""
    paths = installed_paths(state_dir, app_path)
    if require_installed:
        validate_installed_launcher(state_dir, app_path=paths["app_path"])
    state, app = paths["state_dir"], paths["app_path"]
    return {
        "Label": label,
        "ProgramArguments": [
            str(app / "Contents/MacOS" / EXECUTABLE),
            "--launcher-config", str(paths["config_path"]),
        ],
        "WorkingDirectory": str(state),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 0,
        "Umask": 0o077,
        "StandardOutPath": str(state / "vision.log"),
        "StandardErrorPath": str(state / "vision-error.log"),
    }


def make_launcher_config(repo_root, state_dir, *, home=None):
    repo, state = _path(repo_root), _path(state_dir)
    home = _path(home or Path.home())
    return {
        "schema_version": 1,
        "bundle_identifier": BUNDLE_ID,
        "python": str(state / "env/bin/python"),
        "service_script": str(repo / "providers/robot-vision/vision_service.py"),
        "service_config": str(state / "config.json"),
        "state_dir": str(state),
        "working_directory": str(state),
        "environment": {
            "HOME": str(home),
            "PATH": str(state / "env/bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
        },
        "camera_probe": {"enabled": True, "host": "192.168.1.45", "port": 554},
        "show_status_item": True,
    }


def bundle_info(config_path):
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Biome Robot Vision",
        "CFBundleDisplayName": "Biome Robot Vision",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSPrincipalClass": "NSApplication",
        "NSLocalNetworkUsageDescription": PURPOSE,
        "RobotVisionLauncherConfig": str(config_path),
    }


def _bundle_files(app):
    app = Path(app)
    files = {}
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlinks are not expected in this native-only bundle.")
        if path.is_file():
            files[str(path.relative_to(app))] = _sha(path)
    return files


def _read_info(app):
    with (Path(app) / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleExecutable") != EXECUTABLE:
        raise ValueError("Existing bundle has a different application identity; refusing replacement.")
    return info


def signing_identities():
    """Return candidates in memory; callers expose only count and certificate types."""
    if sys.platform != "darwin":
        return []
    result = subprocess.run(
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        capture_output=True, text=True, check=True,
    )
    identities = []
    for line in result.stdout.splitlines():
        match = re.match(r'^\s*\d+\)\s+([0-9a-fA-F]{40})\s+"([^"]+)"', line)
        if match:
            certificate_type = match.group(2).partition(":")[0]
            if certificate_type in ("Developer ID Application", "Apple Development",
                                    "Mac Developer", "Apple Distribution", "3rd Party Mac Developer Application"):
                identities.append({"fingerprint": match.group(1),
                                   "certificate_type": certificate_type})
    preference = {"Developer ID Application": 0, "Apple Development": 1, "Mac Developer": 2}
    return sorted(identities, key=lambda item: (preference.get(item["certificate_type"], 9),
                                               item["fingerprint"]))


def signing_summary(identities):
    types = {}
    for item in identities:
        kind = item["certificate_type"]
        types[kind] = types.get(kind, 0) + 1
    return {"valid_identity_count": len(identities), "types": types}


def _signing_details(app):
    if sys.platform != "darwin":
        return {}
    result = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", "-r-", str(app)],
                            capture_output=True, text=True, check=True)
    fields = {}
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith(("Identifier=", "TeamIdentifier=", "Signature=")):
            key, value = line.split("=", 1)
            fields[key] = value
        else:
            requirement = re.match(r"^\s*(?:#\s*)?designated =>\s*(.+)$", line)
            if requirement:
                fields["DesignatedRequirement"] = requirement.group(1)
    if not fields.get("DesignatedRequirement"):
        raise ValueError("Native code has no reported designated requirement.")
    if fields.get("Identifier") != BUNDLE_ID:
        raise ValueError("Code-signing identifier differs from application identity.")
    return fields


def _verify_signature(app):
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)],
                       capture_output=True, check=True)
        _signing_details(app)


def _validate_payload(manifest, app, config_path, *, state_dir=None, app_path=None):
    if manifest.get("schema_version") != 1 or manifest.get("bundle_identifier") != BUNDLE_ID:
        raise ValueError("Unsupported or mismatched launcher manifest.")
    state = _path(state_dir or manifest["state_dir"])
    final_app = _path(app_path or manifest["app_path"])
    if str(state) != manifest["state_dir"] or str(final_app) != manifest["app_path"]:
        raise ValueError("Prepared state/app destination does not match the requested installation.")
    info = _read_info(app)
    binary = Path(app) / "Contents/MacOS" / EXECUTABLE
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Native launcher is missing or is not executable.")
    expected_config = installed_paths(state, final_app)["config_path"]
    if info.get("RobotVisionLauncherConfig") != str(expected_config):
        raise ValueError("Bundle launcher configuration points to a different state.")
    if _bundle_files(app) != manifest.get("bundle_files"):
        raise ValueError("Native bundle checksum mismatch.")
    if _sha(config_path) != manifest.get("launcher_config_sha256"):
        raise ValueError("Launcher configuration checksum mismatch.")
    config = _private_json(config_path)
    if config.get("bundle_identifier") != BUNDLE_ID or config.get("schema_version") != 1:
        raise ValueError("Configuration identity or version mismatch.")
    if config.get("state_dir") != str(state) or config.get("working_directory") != str(state):
        raise ValueError("Configuration state differs from manifest.")
    for name in ("python", "service_script", "service_config"):
        if not isinstance(config.get(name), str) or not Path(config[name]).is_absolute():
            raise ValueError("Configuration paths must be absolute: " + name)
    if config["service_config"] != str(state / "config.json"):
        raise ValueError("Service config must belong to the selected state.")
    _verify_signature(app)
    if sys.platform == "darwin" and _signing_details(app) != manifest.get("signing_details"):
        raise ValueError("Native code-signing identity differs from the manifest.")
    return manifest


def validate_installed_launcher(state_dir, *, app_path=None):
    paths = installed_paths(state_dir, app_path)
    try:
        manifest = _private_json(paths["manifest_path"])
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Prepare the named macOS app with setup_macos_app.py --prepare, review it, "
            "then use --install-prepared before installing the provider LaunchAgents."
        ) from exc
    return _validate_payload(manifest, paths["app_path"], paths["config_path"],
                             state_dir=paths["state_dir"], app_path=paths["app_path"])


def validate_prepared(prepared_dir):
    prepared = _path(prepared_dir)
    manifest = _private_json(prepared / "manifest.json")
    _validate_payload(manifest, prepared / APP_NAME, prepared / "launcher.json")
    spec = plistlib.loads((prepared / "service.plist").read_bytes())
    if spec != launch_agent_spec(manifest["state_dir"], app_path=manifest["app_path"],
                                 label=manifest["service_label"]):
        raise ValueError("Prepared LaunchAgent does not match the manifest.")
    return manifest


def _input_digest(config, info, source_hash, identity):
    return hashlib.sha256(_json_bytes({
        "config": config, "info": info, "source_sha256": source_hash,
        "identity_selection": identity,
    })).hexdigest()


def deployment_plan(repo_root, state_dir, *, app_path=None, label=SERVICE_LABEL):
    paths = installed_paths(state_dir, app_path)
    return {
        "installed": False,
        "bundle_identifier": BUNDLE_ID,
        "app_path": str(paths["app_path"]),
        "launcher_config_path": str(paths["config_path"]),
        "manifest_path": str(paths["manifest_path"]),
        "launcher_configuration": make_launcher_config(repo_root, state_dir),
        "launch_agent": launch_agent_spec(state_dir, app_path=paths["app_path"], label=label),
        "transition": [
            "Review and verify the staged native bundle and configuration.",
            "Stop the old vision LaunchAgent and observe service.lock released.",
            "Install the prepared bundle/config/manifest; this does not start jobs.",
            "Use install_provider.py to write/start its wrapper-backed service LaunchAgent.",
            "Verify the Python child's real RTSP capture under the LaunchAgent.",
        ],
        "permission": "Use the actual macOS Local Network consent for Biome Robot Vision if requested.",
    }


def prepare_bundle(repo_root, state_dir, output_dir, *, app_path=None,
                   identity="auto", label=SERVICE_LABEL):
    if sys.platform != "darwin":
        raise RuntimeError("Native preparation requires macOS and the Swift toolchain.")
    paths = installed_paths(state_dir, app_path)
    source = _path(repo_root) / "providers/robot-vision/macos/RobotVisionApp.swift"
    config = make_launcher_config(repo_root, state_dir)
    info = bundle_info(paths["config_path"])
    source_hash = _sha(source)
    digest = _input_digest(config, info, source_hash, identity)
    output = _path(output_dir)
    if (output == paths["app_path"] or output == paths["state_dir"] or
            output in paths["app_path"].parents or paths["app_path"] in output.parents):
        raise ValueError("Preparation must use an isolated output directory.")
    if output.exists():
        manifest = validate_prepared(output)
        if manifest.get("input_sha256") != digest:
            raise ValueError("Prepared output has different inputs; choose a fresh output directory.")
        return {"prepared": True, "reused": True, "output_dir": str(output), "manifest": manifest}

    existing = None
    if paths["app_path"].exists():
        _read_info(paths["app_path"])
        existing = validate_installed_launcher(state_dir, app_path=paths["app_path"])
        if existing.get("signing_mode") == "development-ad-hoc" and existing.get("input_sha256") != digest:
            raise ValueError("Refusing to silently rebuild an installed ad-hoc identity. A stable signing migration requires explicit review.")

    identities = signing_identities()
    if identity == "auto":
        selected = identities[0] if identities else None
    elif identity == "adhoc":
        selected = None
    else:
        selected = next((item for item in identities if item["fingerprint"].lower() == identity.lower()), None)
        if selected is None:
            raise ValueError("Requested signing identity is not an available valid application identity.")
    signing_mode = "apple-issued" if selected else "development-ad-hoc"
    if existing and existing.get("signing_mode") != signing_mode:
        raise ValueError("Existing application uses a different signing mode; explicit migration review is required.")

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _owned_directory(output.parent)
    temporary = Path(tempfile.mkdtemp(prefix="." + output.name + ".", dir=output.parent))
    try:
        app = temporary / APP_NAME
        if existing and existing.get("input_sha256") == digest:
            shutil.copytree(paths["app_path"], app)
        else:
            (app / "Contents/MacOS").mkdir(parents=True, mode=0o755)
            (app / "Contents").chmod(0o755)
            app.chmod(0o755)
            _atomic(app / "Contents/Info.plist", plistlib.dumps(info), 0o644)
            binary = app / "Contents/MacOS" / EXECUTABLE
            subprocess.run(["/usr/bin/xcrun", "swiftc", "-swift-version", "5", "-O",
                            "-framework", "AppKit", "-framework", "Network",
                            str(source), "-o", str(binary)], check=True)
            binary.chmod(0o755)
            signing_argument = selected["fingerprint"] if selected else "-"
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", signing_argument,
                            "--identifier", BUNDLE_ID, "--timestamp=none", str(app)],
                           capture_output=True, check=True)
        details = _signing_details(app)
        if existing and existing.get("signing_mode") == "apple-issued":
            old_team = existing.get("signing_details", {}).get("TeamIdentifier")
            if (not old_team or old_team != details.get("TeamIdentifier") or
                    existing.get("signing_details") != details):
                raise ValueError("Prepared native executable has a different designated code identity.")
        uuid_result = subprocess.run(
            ["/usr/bin/xcrun", "dwarfdump", "--uuid", str(app / "Contents/MacOS" / EXECUTABLE)],
            capture_output=True, text=True, check=True)
        uuids = re.findall(r"UUID:\s+([0-9A-Fa-f-]{36})", uuid_result.stdout)
        if not uuids:
            raise ValueError("Native main executable has no build UUID.")
        _atomic(temporary / "launcher.json", _json_bytes(config))
        manifest = {
            "schema_version": 1, "bundle_identifier": BUNDLE_ID,
            "state_dir": str(paths["state_dir"]), "app_path": str(paths["app_path"]),
            "service_label": label, "source_sha256": source_hash, "input_sha256": digest,
            "bundle_files": _bundle_files(app),
            "launcher_config_sha256": _sha(temporary / "launcher.json"),
            "signing_mode": signing_mode,
            "signing_certificate_type": selected["certificate_type"] if selected else None,
            "signing_details": details, "native_build_uuids": uuids,
            "signing_availability": signing_summary(identities),
            "limitations": [AD_HOC_LIMITATION] if not selected else [],
            "qualification": {"native_compiled": True, "background_camera_capture": "not_qualified",
                              "local_network_consent": "not_assumed"},
        }
        _atomic(temporary / "manifest.json", _json_bytes(manifest))
        _atomic(temporary / "service.plist",
                plistlib.dumps(launch_agent_spec(state_dir, app_path=paths["app_path"], label=label)))
        validate_prepared(temporary)
        os.replace(temporary, output)
        return {"prepared": True, "reused": False, "output_dir": str(output), "manifest": manifest}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


@contextmanager
def _exclusive_lock(path):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise ValueError("Unsafe ownership lock.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Vision is still running; stop its job and wait for graceful quiescence.") from exc
        yield
    finally:
        os.close(fd)


def _require_service_unloaded(label):
    """A legacy process may predate service.lock; require its actual job unloaded."""
    if sys.platform != "darwin":
        return
    domain = "gui/" + str(os.getuid())
    context = subprocess.run(["/bin/launchctl", "print", domain], capture_output=True, text=True)
    if context.returncode != 0:
        raise RuntimeError("Cannot verify the user's GUI launchd context; no installation performed.")
    result = subprocess.run(["/bin/launchctl", "print", domain + "/" + label],
                            capture_output=True, text=True)
    if result.returncode == 0:
        raise RuntimeError("The vision LaunchAgent is still loaded. Boot out its job and observe graceful exit before installing the app.")
    if "Could not find service" not in result.stderr:
        raise RuntimeError("Unable to establish that the old vision LaunchAgent is absent; no installation performed.")


def install_prepared(prepared_dir, *, state_dir=None, app_path=None):
    """Install a verified stage without touching launchd or permission settings."""
    prepared = _path(prepared_dir)
    manifest = validate_prepared(prepared)
    state = _path(state_dir or manifest["state_dir"])
    app = _path(app_path or manifest["app_path"])
    _validate_payload(manifest, prepared / APP_NAME, prepared / "launcher.json",
                      state_dir=state, app_path=app)
    paths = installed_paths(state, app)
    if app.exists():
        _read_info(app)
        existing = validate_installed_launcher(state, app_path=app)
        if existing == manifest:
            return {"installed": True, "unchanged": True, "app_path": str(app),
                    "state_dir": str(state), "jobs_started": False}
        if (existing.get("signing_mode") != manifest.get("signing_mode") or
                existing.get("signing_details") != manifest.get("signing_details")):
            raise ValueError("Refusing to replace a different code-signing identity.")
        if existing.get("signing_mode") == "development-ad-hoc":
            raise ValueError("Refusing an unreviewed rebuild of the installed ad-hoc identity.")
    _require_service_unloaded(manifest["service_label"])
    _owned_directory(state)
    _owned_directory(app.parent)
    _owned_directory(paths["manifest_path"].parent)
    with ExitStack() as stack:
        stack.enter_context(_exclusive_lock(state / "launcher.lock"))
        stack.enter_context(_exclusive_lock(state / "service.lock"))
        token = uuid.uuid4().hex
        candidate = app.parent / ("." + app.name + "." + token + ".new")
        backup = app.parent / ("." + app.name + "." + token + ".old")
        old_config = paths["config_path"].read_bytes() if paths["config_path"].exists() else None
        old_manifest = paths["manifest_path"].read_bytes() if paths["manifest_path"].exists() else None
        replaced = False
        try:
            shutil.copytree(prepared / APP_NAME, candidate)
            _validate_payload(manifest, candidate, prepared / "launcher.json",
                              state_dir=state, app_path=app)
            if app.exists():
                os.replace(app, backup)
            os.replace(candidate, app)
            replaced = True
            _atomic(paths["config_path"], (prepared / "launcher.json").read_bytes())
            _atomic(paths["manifest_path"], (prepared / "manifest.json").read_bytes())
            validate_installed_launcher(state, app_path=app)
        except Exception:
            if replaced and app.exists():
                shutil.rmtree(app)
            if backup.exists():
                os.replace(backup, app)
            for path, previous in ((paths["config_path"], old_config),
                                   (paths["manifest_path"], old_manifest)):
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, previous)
            raise
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
        if backup.exists():
            shutil.rmtree(backup)
    return {"installed": True, "unchanged": False, "app_path": str(app),
            "state_dir": str(state), "jobs_started": False,
            "signing_mode": manifest["signing_mode"], "limitations": manifest["limitations"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=HERE.parents[1])
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--app-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--identity", default="auto", help="auto, adhoc, or an available certificate fingerprint")
    parser.add_argument("--label", default=SERVICE_LABEL)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--verify-prepared", type=Path)
    action.add_argument("--install-prepared", type=Path)
    args = parser.parse_args(argv)
    if args.prepare:
        if args.output_dir is None:
            parser.error("--prepare requires a fresh --output-dir")
        result = prepare_bundle(args.repo_root, args.state_dir, args.output_dir,
                                app_path=args.app_path, identity=args.identity, label=args.label)
    elif args.verify_prepared:
        result = {"verified": True, "manifest": validate_prepared(args.verify_prepared)}
    elif args.install_prepared:
        result = install_prepared(args.install_prepared, state_dir=args.state_dir, app_path=args.app_path)
    else:
        result = deployment_plan(args.repo_root, args.state_dir,
                                 app_path=args.app_path, label=args.label)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
