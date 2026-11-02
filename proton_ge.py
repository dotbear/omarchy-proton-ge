#!/usr/bin/env python3
"""Steam Proton-GE updater. Python standard library only; no source builds."""

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
ASSETS = "https://github.com/GloriousEggroll/proton-ge-custom/releases/download/"
TITLE = "Proton-GE Latest"
TAG = re.compile(r"GE-Proton\d+-\d+(?:-\d+)?\Z")
MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 8 * 1024**3
MAX_MEMBERS = 150000
BUSY = {"checking", "downloading", "extracting"}


def read_json(path, limit=32768):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"JSON file exceeds {limit} bytes")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".proton-ge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def exchange(left, right):
    """Linux atomic directory exchange: Latest is never absent or partial."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def version(directory):
    if not directory.is_dir():
        return ""
    for name in (".omarchy-proton-ge.json", ".protonplus"):
        path = directory / name
        if path.exists():
            tag = read_json(path).get("tag", "")
            if isinstance(tag, str) and TAG.fullmatch(tag):
                return tag
            raise ValueError("Invalid installed Proton-GE metadata")
    path = directory / "version"
    if path.exists():
        with path.open() as stream:
            line = stream.read(256)
        match = re.search(r"\b(GE-Proton\d+-\d+(?:-\d+)?)\b", line)
        if match:
            return match[1]
    raise ValueError("Cannot identify the installed Proton-GE version")


def processes(proc=Path("/proc")):
    """Read only our processes, without launching commands or exposing env."""
    steam, gaming = False, False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            comm = (entry / "comm").read_text().strip().lower()
            steam |= comm == "steam"
            if "wine" in comm or comm == "proton":
                gaming = True
            # Wine can rename a process after the game executable; its
            # inherited Steam/Wine environment still identifies it.
            env = (entry / "environ").read_bytes().split(b"\0")
            gaming |= any(v.startswith((b"STEAM_COMPAT_DATA_PATH=", b"WINEPREFIX=")) for v in env)
        except (OSError, ProcessLookupError):
            continue
    return steam, gaming


# Small, strict text-VDF reader. Value spans let us change only tool names,
# preserving all unrelated configuration, comments, and formatting.
TOKEN = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')


class VdfNode:
    def __init__(self, token, value=None, children=None, closing=0):
        self.key = token[0]
        self.key_span = token[1:]
        self.value = value
        self.children = children
        self.closing = closing

    def child(self, key):
        found = [n for n in self.children or [] if n.key.lower() == key.lower()]
        if len(found) != 1:
            raise ValueError(f"Expected one VDF section: {key}")
        return found[0]


def parse_vdf(text):
    tokens = []
    offset = 0
    for match in TOKEN.finditer(text):
        if match.start() != offset:
            raise ValueError("Malformed VDF token")
        offset = match.end()
        raw = match[0]
        if raw.isspace() or raw.startswith("//"):
            continue
        value = re.sub(r'\\(["\\])', r'\1', raw[1:-1]) if raw.startswith('"') else raw
        tokens.append((value, match.start(), match.end()))
    if offset != len(text):
        raise ValueError("Incomplete VDF input")
    index = 0

    def block(depth=0):
        nonlocal index
        if depth > 64:
            raise ValueError("VDF nesting is too deep")
        nodes = []
        while index < len(tokens) and tokens[index][0] != "}":
            key = tokens[index]
            index += 1
            if key[0] in ("{", "}") or index == len(tokens):
                raise ValueError("Invalid VDF key")
            value = tokens[index]
            index += 1
            if value[0] == "{":
                children = block(depth + 1)
                if index == len(tokens) or tokens[index][0] != "}":
                    raise ValueError("Unclosed VDF section")
                nodes.append(VdfNode(key, children=children, closing=tokens[index][1]))
                index += 1
            elif value[0] == "}":
                raise ValueError("Missing VDF value")
            else:
                nodes.append(VdfNode(key, value=value))
        return nodes

    root = VdfNode(("root", 0, 0), children=block())
    if index != len(tokens):
        raise ValueError("Unexpected closing VDF brace")
    return root


def steam_selections(text):
    root = parse_vdf(text)
    steam = root
    for name in ("InstallConfigStore", "Software", "Valve", "Steam"):
        steam = steam.child(name)
    mappings = [n for n in steam.children if n.key.lower() == "compattoolmapping"]
    edits = []
    count = 0
    default = '\n\t\t\t\t\t"0" { "name" "Proton-GE Latest" "config" "" "priority" "75" }\n\t\t\t\t'
    if not mappings:
        edits.append((steam.closing, steam.closing, '\n\t\t\t\t"CompatToolMapping" {' + default + '}\n\t\t\t'))
        count = 1
    else:
        if len(mappings) != 1 or mappings[0].children is None:
            raise ValueError("Invalid Steam compatibility mappings")
        mapping = mappings[0]
        for entry in mapping.children:
            if not entry.key.isdigit():
                raise ValueError("Invalid Steam game id")
            name = entry.child("name").value
            if name is None:
                raise ValueError("Missing Steam tool name")
            if name[0] != TITLE:
                edits.append((name[1], name[2], json.dumps(TITLE)))
                count += 1
        if not any(n.key == "0" for n in mapping.children):
            edits.append((mapping.closing, mapping.closing, default))
            count += 1
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    parse_vdf(text)
    return text, count


def latest_release():
    with request(API) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Release metadata is too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Expected release metadata object")
    tag = data.get("tag_name", "")
    if not isinstance(tag, str) or not TAG.fullmatch(tag) or data.get("draft") or data.get("prerelease"):
        raise ValueError("No eligible stable Proton-GE release")
    arch = platform.machine()
    if arch not in ("x86_64", "aarch64"):
        raise ValueError("Unsupported CPU architecture")
    assets = data.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 32 or not all(isinstance(a, dict) for a in assets):
        raise ValueError("Invalid release asset list")
    names = [f"{tag}-{arch}.tar.gz"]
    if arch == "x86_64":
        names.append(f"{tag}.tar.gz")
    matches = [a for name in names for a in assets if a.get("name") == name]
    if not matches:
        raise ValueError("Release has no compatible prebuilt archive")
    asset = matches[0]
    digest = asset.get("digest", "")
    size = asset.get("size", 0)
    if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        raise ValueError("Invalid archive size")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Release has no SHA-256 digest")
    expected = ASSETS + tag + "/" + asset["name"]
    if asset.get("browser_download_url") != expected:
        raise ValueError("Unexpected archive URL")
    release = {"tag": tag, "url": expected, "size": size, "digest": digest[7:], "id": str(int(data["id"]))}
    validate_release(release)
    return release


def validate_release(release):
    if not isinstance(release, dict):
        raise ValueError("Invalid cached release")
    tag = release.get("tag", "")
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise ValueError("Invalid release tag")
    arch = platform.machine()
    names = [f"{tag}-{arch}.tar.gz"]
    if arch == "x86_64":
        names.append(f"{tag}.tar.gz")
    if release.get("url") not in [ASSETS + tag + "/" + name for name in names]:
        raise ValueError("Unexpected archive URL")
    if not isinstance(release.get("size"), int) or not 0 < release["size"] <= MAX_ARCHIVE:
        raise ValueError("Invalid archive size")
    if not isinstance(release.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", release["digest"]):
        raise ValueError("Invalid archive checksum")
    if not isinstance(release.get("id"), str) or not re.fullmatch(r"\d{1,20}", release["id"]):
        raise ValueError("Invalid release id")


class GithubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"):
            raise ValueError("Unexpected download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Omarchy-Proton-GE/0.1", "Accept": "application/vnd.github+json" if url == API else "application/octet-stream"})
    return urllib.request.build_opener(GithubRedirects()).open(req, timeout=30)


def download(release, destination, progress):
    digest = hashlib.sha256()
    total, last = 0, 0
    deadline = time.monotonic() + 1800
    with request(release["url"]) as response, destination.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise TimeoutError("Download exceeded 30 minutes")
            total += len(chunk)
            if total > release["size"]:
                raise ValueError("Archive exceeds advertised size")
            output.write(chunk)
            digest.update(chunk)
            if time.monotonic() - last > 1:
                progress(round(100 * total / release["size"]))
                last = time.monotonic()
    if total != release["size"] or digest.hexdigest() != release["digest"]:
        raise ValueError("Archive checksum or size mismatch")


def extract(archive, destination):
    """Bound tar entries/size and apply Python's safe data extraction filter."""
    total, count = 0, 0
    with tarfile.open(archive, "r:gz") as tar:
        def members():
            nonlocal total, count
            for member in tar:
                total += member.size
                count += 1
                if count > MAX_MEMBERS or total > MAX_EXPANDED or member.size < 0:
                    raise ValueError("Archive expansion limit exceeded")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Unsafe archive path")
                yield member
        tar.extractall(destination, members=members(), filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
        raise ValueError("Expected one Proton-GE archive directory")
    root = roots[0]
    for name in ("proton", "compatibilitytool.vdf", "version"):
        if not (root / name).is_file() or (root / name).is_symlink():
            raise ValueError(f"Release is missing {name}")
    return root


def manifest_for_latest(text):
    root = parse_vdf(text).child("compatibilitytools").child("compat_tools")
    if len(root.children) != 1:
        raise ValueError("Expected exactly one Steam compatibility tool")
    tool = root.children[0]
    display = tool.child("display_name").value
    for start, end in sorted([tool.key_span, (display[1], display[2])], reverse=True):
        text = text[:start] + json.dumps(TITLE) + text[end:]
    parse_vdf(text)
    return text


class Updater:
    def __init__(self, home=None, state_dir=None, steam=None):
        home = Path(home or Path.home())
        self.directory = Path(state_dir or Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "dotbear.proton-ge")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.json"
        self.state = read_json(self.path) if self.path.exists() else {"auto": True, "phase": "idle", "checked_at": 0}
        if not isinstance(self.state.get("auto"), bool):
            raise ValueError("Invalid automatic-update preference")
        checked_at = self.state.get("checked_at", 0)
        if not isinstance(checked_at, (int, float)) or not 0 <= checked_at <= time.time() + 60:
            self.state["checked_at"] = 0
        data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
        candidates = [data / "Steam", home / ".steam/steam", home / ".steam/root"]
        self.steam = Path(steam) if steam else next((p.resolve() for p in candidates if (p / "config/config.vdf").is_file()), candidates[0])
        if not (self.steam / "config/config.vdf").is_file():
            raise ValueError("Native Steam installation not found")
        self.current = self.steam / "compatibilitytools.d" / TITLE
        self.storage = self.steam / ".omarchy-proton-ge"
        self.previous = self.storage / "previous"
        for path in (self.current, self.storage, self.previous):
            if path.is_symlink():
                raise ValueError("Managed installation paths must not be symlinks")

    def save(self, **updates):
        self.state.update(updates)
        self.state["installed"] = version(self.current)
        self.state["previous"] = version(self.previous)
        atomic_write(self.path, (json.dumps(self.state, ensure_ascii=False) + "\n").encode())

    def sync_selections(self):
        config = self.steam / "config/config.vdf"
        original = config.read_bytes()
        if len(original) > 8 * 1024**2:
            raise ValueError("Steam configuration exceeds 8 MiB")
        updated, count = steam_selections(original.decode())
        if count and processes()[0]:
            self.state.update(selections_pending=True, selections_ready=False)
            return
        if count:
            backup = self.directory / "steam-config.before-proton-ge.vdf"
            if not backup.exists():
                atomic_write(backup, original)
            if processes()[0] or config.read_bytes() != original:
                self.state.update(selections_pending=True, selections_ready=False)
                return
            atomic_write(config, updated.encode())
        self.state.update(selections_pending=False, selections_ready=True)

    def check(self):
        self.save(phase="checking", error="", checked_at=time.time())
        release = latest_release()
        self.save(latest=release["tag"], release=release, phase="current" if release["tag"] == version(self.current) else "available")
        return release

    def install(self, release):
        validate_release(release)
        if release["tag"] == version(self.current):
            self.save(phase="current")
            return
        if processes()[1]:
            self.save(phase="deferred")
            return
        self.storage.mkdir(exist_ok=True)
        self.current.parent.mkdir(exist_ok=True)
        # Both the staged release and one previous version must fit. Never
        # reclaim the current/previous installation to make room for a download.
        if shutil.disk_usage(self.storage).free < MAX_EXPANDED + release["size"]:
            raise OSError("At least 8 GiB plus the download size must be free")
        for orphan in self.storage.glob("stage-*"):
            if orphan.is_dir() and not orphan.is_symlink():
                shutil.rmtree(orphan)
        with tempfile.TemporaryDirectory(prefix="stage-", dir=self.storage) as work:
            work = Path(work)
            archive = work / "release.tar.gz"
            self.save(phase="downloading", progress=0, error="")
            download(release, archive, lambda p: self.save(progress=p))
            self.save(phase="extracting")
            root = extract(archive, work / "extracted")
            if version(root) != release["tag"]:
                raise ValueError("Archive version does not match the release")
            manifest = root / "compatibilitytool.vdf"
            atomic_write(manifest, manifest_for_latest(manifest.read_text()).encode())
            if (self.current / "user_settings.py").is_file():
                (root / "user_settings.py").unlink(missing_ok=True)
                shutil.copy2(self.current / "user_settings.py", root / "user_settings.py", follow_symlinks=False)
            metadata = {"runner_endpoint": API.removesuffix("/latest"), "runner_title": "Proton-GE", "tag": release["tag"], "provider_id": "proton-ge", "tool_id": "steam-system/proton/proton-ge", "launcher_id": "steam-system", "variant_id": "x86" if platform.machine() == "x86_64" else "aarch64", "release_id": release["id"]}
            atomic_write(root / ".protonplus", json.dumps(metadata).encode())
            # Recheck after download/extraction: a game may have started while
            # the archive was being prepared. No running game is interrupted.
            if processes()[1]:
                self.save(phase="deferred")
                return
            if self.current.exists():
                # Keep the exchanged directory outside TemporaryDirectory so
                # an exception cannot delete the previous working installation.
                candidate = self.storage / "candidate"
                if candidate.exists():
                    raise ValueError("An unfinished installation needs recovery")
                os.replace(root, candidate)
                atomic_write(self.storage / "transaction.json", json.dumps({"tag": release["tag"]}).encode())
                exchange(self.current, candidate)
                self.recover()
            else:
                os.replace(root, self.current)
            self.save(phase="updated", restart_required=processes()[0], updated_at=time.time(), error="")

    def recover(self):
        transaction = self.storage / "transaction.json"
        candidate = self.storage / "candidate"
        if candidate.is_symlink():
            raise ValueError("Installation candidate must not be a symlink")
        if not transaction.exists():
            # Crash before writing the transaction: candidate is the new build;
            # the working current and previous directories are still intact.
            if candidate.exists():
                shutil.rmtree(candidate)
            return
        tag = read_json(transaction).get("tag", "")
        if not isinstance(tag, str) or not TAG.fullmatch(tag):
            raise ValueError("Invalid installation transaction")
        if candidate.exists():
            if version(self.current) == tag:
                if self.previous.exists():
                    shutil.rmtree(self.previous)
                os.replace(candidate, self.previous)
                self.state["restart_required"] = processes()[0]
            else:
                shutil.rmtree(candidate)
        transaction.unlink()

    def rollback(self):
        if processes()[1]:
            raise ValueError("Close Proton games before restoring the previous version")
        if not self.previous.is_dir() or not self.current.is_dir():
            raise ValueError("No previous version to restore")
        exchange(self.current, self.previous)
        self.save(phase="rollback", auto=False, restart_required=processes()[0], error="")

    def run(self, command):
        self.recover()
        self.sync_selections()
        if not processes()[0]:
            self.state["restart_required"] = False
        if command == "pause":
            self.state["auto"] = False
            if self.state.get("phase") == "deferred":
                self.state["phase"] = "available"
        elif command == "resume":
            self.state["auto"] = True
            self.state["checked_at"] = 0
        elif command == "rollback":
            self.rollback()
        elif command in ("check", "update"):
            release = self.check()
            if command == "update":
                self.install(release)
        elif command == "tick":
            if self.state.get("phase") in BUSY:
                self.state.update(phase="error", error="The previous update was interrupted. Check again to retry.")
            if self.state.get("auto", True) and time.time() - self.state.get("checked_at", 0) >= 3600:
                self.install(self.check())
            elif self.state.get("phase") == "deferred":
                self.install(self.state["release"])
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["tick", "check", "update", "rollback", "pause", "resume", "status", "sync"])
    args = parser.parse_args()
    directory = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dotbear.proton-ge"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.command != "tick":
                print("Another Proton-GE operation is running", file=sys.stderr)
            return
        updater = Updater(state_dir=directory)
        try:
            if args.command != "status":
                updater.run(args.command)
            print(json.dumps(updater.state, ensure_ascii=False))
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, socket.timeout) as error:
            updater.save(phase="error", error=str(error)[:500])
            print(str(error)[:500], file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
