#!/usr/bin/env python3
"""Check or update the Termux installation over an explicitly pinned SSH connection."""

from __future__ import annotations

import argparse
import base64
import io
import ipaddress
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile

import cerro


HOST_KEY_ALIAS = "cerro-phone"
CODE_FILES = (*cerro.PAYLOAD, "cerro.py")

# The helper executes under Termux's Python. Only code is uploaded; the model and
# API key remain on the phone. No downloaded script or remote output is evaluated
# by the host shell, and no device log or credential is returned to Actions.
REMOTE_HELPER = r'''
import contextlib
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile

FILES = ("bin/llama-server", "device/run-server.sh", "provenance.json", "cerro.py", "bin/llama-bench")
REQUIRED = set(FILES[:-1])

class Failure(Exception):
    pass

def device_identity():
    values = []
    for name in ("ro.product.model", "ro.product.device", "ro.product.cpu.abilist", "ro.build.version.sdk"):
        result = subprocess.run(["/system/bin/getprop", name], capture_output=True, timeout=10)
        if result.returncode:
            raise Failure("Could not read Android device properties.")
        values.append(result.stdout.decode("utf-8", errors="replace").strip())
    model, device, abis, sdk = values
    if model != "NX721J" and device not in ("cerro", "PQ83A01"):
        raise Failure("Refusing this device: expected Nubia NX721J / cerro / PQ83A01.")
    if "arm64-v8a" not in abis.split(",") or not sdk.isdigit() or int(sdk) < 28:
        raise Failure("The kit requires arm64-v8a Android API 28 or newer.")

def safe_layout(root):
    for name in ("", "bin", "device", "config", "models"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise Failure("A server directory is a symlink or not a directory.")
    for name in (*FILES, "models/model.gguf", "config/api-key", "server.pid", ".deploying"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise Failure("A server file is a symlink or not a regular file.")

def model_ready(root):
    path = root / "models/model.gguf"
    if not path.is_file():
        raise Failure("Copy a local GGUF model to ~/cerro-server/models/model.gguf before deploy.")
    with path.open("rb") as source:
        if source.read(4) != b"GGUF":
            raise Failure("The phone's models/model.gguf does not have a GGUF header.")

@contextlib.contextmanager
def lock(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise Failure("Another control/deploy operation is active, or a stale lock needs inspection on the phone.") from error
    try:
        yield
    finally:
        path.rmdir()

def control(root, action, launcher=None):
    launcher = launcher or root / "device/run-server.sh"
    if not launcher.is_file():
        if action == "status":
            return False
        raise Failure("Kit is not installed; use deploy first.")
    environment = dict(os.environ, CERRO_SERVER_DIR=str(root))
    result = subprocess.run(["sh", str(launcher), action], env=environment,
                            capture_output=True, timeout=30)
    if action == "status" and result.returncode in (0, 3):
        return result.returncode == 0
    if result.returncode:
        raise Failure("Server " + action + " failed; inspect ~/cerro-server/server.log and locks on the phone.")
    return True

def probe(root, wait_seconds):
    result = subprocess.run([sys.executable, str(root / "cerro.py"), "probe",
                             "--key-file", str(root / "config/api-key"),
                             "--wait-seconds", str(wait_seconds)],
                            capture_output=True, timeout=wait_seconds + 160)
    if result.returncode:
        raise Failure("Readiness, API authentication or inference failed; inspect server.log on the phone.")
    if not control(root, "status"):
        raise Failure("The newly started process exited; an older listener may own port 8080.")

def require_free_port(port=8080):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise Failure("Port 8080 is occupied. Stop the old ADB or manually launched Termux server before deploy.") from error

def unpack(source, stage):
    allowed = set(FILES) | {"SHA256SUMS"}
    seen, total = set(), 0
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            total += member.size
            if (member.name not in allowed or member.name in seen or not member.isfile()
                    or member.size < 0 or member.size > 128 * 1024 * 1024
                    or total > 256 * 1024 * 1024):
                raise Failure("Invalid deployment archive; no code was installed.")
            seen.add(member.name)
            target = stage / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    if not (REQUIRED | {"SHA256SUMS"}).issubset(seen):
        raise Failure("Deployment archive lacks required code.")
    expected = {}
    for line in (stage / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] not in set(FILES) or match[2] in expected:
            raise Failure("Invalid deployment checksum manifest.")
        expected[match[2]] = match[1]
    if set(expected) != seen - {"SHA256SUMS"}:
        raise Failure("Deployment file list does not match its manifest.")
    for name, expected_digest in expected.items():
        digest = hashlib.sha256()
        with (stage / name).open("rb") as incoming:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected_digest:
            raise Failure("Transferred code checksum mismatch; no code was installed.")

def ensure_key(root):
    directory = root / "config"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "api-key"
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as target:
            target.write(secrets.token_hex(32) + "\n")
    if not re.fullmatch(rb"[0-9a-f]{64}\n?", path.read_bytes()):
        raise Failure("Existing API key is invalid; it was not replaced.")
    path.chmod(0o600)

def replace_code(root, source, launcher):
    with lock(root / ".control-lock"):
        if control(root, "status", launcher):
            raise Failure("Server started during deployment; retry after stopping concurrent control operations.")
        (root / ".deploying").touch(mode=0o600)
        for directory in (root, root / "bin", root / "device"):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        for name in FILES:
            destination = root / name
            original = source / name
            if original.is_file():
                # Copy within the same private directory, then rename; the source
                # copy remains available for rollback and process identification.
                temporary = destination.with_name(destination.name + ".next")
                if temporary.exists() or temporary.is_symlink():
                    raise Failure("A previous .next file needs inspection on the phone.")
                try:
                    with original.open("rb") as incoming, temporary.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    temporary.chmod(0o700 if name.startswith(("bin/", "device/")) else 0o600)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                destination.unlink(missing_ok=True)
        (root / "server.pid").unlink(missing_ok=True)
        (root / ".deploying").unlink()

def deploy(root, source, wait_seconds):
    safe_layout(root)
    model_ready(root)  # Fail before creating keys, staging, or stopping a server.
    with lock(root / ".ssh-deploy-lock"):
        stage = Path(tempfile.mkdtemp(prefix=".ssh-stage-", dir=root))
        remove_stage = True
        try:
            unpack(source, stage)
            ensure_key(root)
            backup = stage / "previous"
            backup.mkdir(mode=0o700)
            for name in FILES:
                existing = root / name
                if existing.is_file():
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(existing, saved)
            was_running = control(root, "status")
            if (root / "device/run-server.sh").is_file():
                control(root, "stop")
            launcher = stage / "device/run-server.sh"
            try:
                require_free_port()
                replace_code(root, stage, launcher)
                control(root, "start")
                probe(root, wait_seconds)
            except Exception as error:
                try:
                    control(root, "stop", launcher)
                    replace_code(root, backup, launcher)
                    if was_running:
                        control(root, "start")
                except Exception as rollback_error:
                    remove_stage = False
                    # Fail closed if interrupted/restoring failed. The backup is
                    # retained under .ssh-stage-*/previous for manual recovery.
                    (root / ".deploying").touch(mode=0o600)
                    raise Failure("Deployment and rollback failed. Backup retained in ~/cerro-server/.ssh-stage-*/previous; inspect the phone before restarting.") from rollback_error
                raise Failure("Deployment failed; previous code and previous running/stopped state restored. " + str(error)) from error
        finally:
            if remove_stage:
                shutil.rmtree(stage)
    print("PASS: deployed; existing model/key preserved; ready; unauthenticated inference rejected; authenticated inference generated text.")

def main():
    os.umask(0o077)
    action = sys.argv[1]
    wait_seconds = int(sys.argv[2])
    device_identity()
    root = Path.home() / "cerro-server"
    safe_layout(root)
    if action == "check":
        print("PASS: pinned SSH connection; supported NX721J/cerro ARM64 Android device; Termux Python available.")
        print("Model: " + ("present" if (root / "models/model.gguf").is_file() else "missing; copy GGUF before deploy") + ".")
        print("Server process: " + ("running" if control(root, "status") else "stopped or not installed") + ".")
    elif action == "deploy":
        deploy(root, sys.stdin.buffer, wait_seconds)
    elif action == "start":
        model_ready(root)
        if not control(root, "status"):
            require_free_port()
        control(root, "start")
        probe(root, wait_seconds)
        print("PASS: server ready; authentication and inference verified.")
    elif action == "stop":
        control(root, "stop")
        print("Server stopped.")
    elif action == "status":
        if control(root, "status"):
            print("Server process is running. Use start or deploy to verify inference.")
        else:
            print("Server process is stopped or not installed.")
            return 3
    else:
        raise Failure("Unsupported phone operation.")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Failure, OSError, ValueError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        if isinstance(error, Failure):
            print("Error: " + str(error), file=sys.stderr)
        else:
            print("Error: phone operation failed; inspect local files, locks and server.log on the phone.", file=sys.stderr)
        sys.exit(1)
'''


def credential_file(path: Path, *, private: bool = False) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise cerro.KitError("SSH credentials must be regular files, not symlinks.")
    if any(character in str(path) for character in ("\x00", "\n", "\r", "$", "%")):
        raise cerro.KitError("SSH credential paths cannot contain NUL, newlines, '$', or '%'.")
    if private and os.name != "nt" and path.stat().st_mode & 0o077:
        raise cerro.KitError("SSH private-key permissions are too broad; use chmod 600.")
    return path


def validate_host_pin(path: Path) -> None:
    lines = [line for line in path.read_text(encoding="ascii").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise cerro.KitError("known_hosts must contain one explicit cerro-phone Ed25519 host key.")
    fields = lines[0].split()
    if len(fields) < 3 or fields[:2] != [HOST_KEY_ALIAS, "ssh-ed25519"]:
        raise cerro.KitError("Copy the 'cerro-phone ssh-ed25519 ...' host-key line from the phone bootstrap.")
    try:
        wire = base64.b64decode(fields[2], validate=True)
    except ValueError as error:
        raise cerro.KitError("Malformed Ed25519 host key.") from error
    if len(wire) != 51 or wire[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise cerro.KitError("Malformed Ed25519 host key.")


class Ssh:
    def __init__(self, host: str, user: str, key_file: Path, known_hosts: Path,
                 port: int = 8022, executable: str = "ssh"):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                          for label in host.rstrip(".").split("."))):
                raise cerro.KitError("SSH host must be an IP address or DNS name, without shell syntax.")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", user):
            raise cerro.KitError("Invalid SSH username; use the phone bootstrap's whoami value.")
        if not 1 <= port <= 65535:
            raise cerro.KitError("SSH port must be between 1 and 65535.")
        self.host, self.user, self.port, self.executable = host, user, port, executable
        self.key_file = credential_file(key_file, private=True)
        self.known_hosts = credential_file(known_hosts)
        validate_host_pin(self.known_hosts)

    def argv(self) -> list[str]:
        # OpenSSH parses -o values itself; quote spaces in the known_hosts path.
        known = str(self.known_hosts).replace("\\", "\\\\").replace('"', '\\"')
        options = (
            "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
            "PreferredAuthentications=publickey", "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no", "ForwardAgent=no", "ForwardX11=no",
            "ClearAllForwardings=yes", "PermitLocalCommand=no", "ProxyCommand=none",
            "ControlMaster=no", "ControlPath=none", "StrictHostKeyChecking=yes",
            "HostKeyAlias=" + HOST_KEY_ALIAS, "HostKeyAlgorithms=ssh-ed25519",
            'UserKnownHostsFile="' + known + '"', "GlobalKnownHostsFile=" + os.devnull,
            "VerifyHostKeyDNS=no", "CheckHostIP=no", "UpdateHostKeys=no",
            "ConnectTimeout=20", "ServerAliveInterval=15", "ServerAliveCountMax=3",
            "LogLevel=ERROR",
        )
        command = [self.executable, "-F", os.devnull, "-T", "-p", str(self.port),
                   "-l", self.user, "-i", str(self.key_file)]
        for option in options:
            command.extend(("-o", option))
        command.append(self.host)
        return command

    def run(self, action: str, *, payload: bytes | None = None,
            wait_seconds: int = 180) -> int:
        if action not in ("check", "deploy", "start", "stop", "status"):
            raise cerro.KitError("Unsupported phone operation.")
        if not 0 <= wait_seconds <= 1800:
            raise cerro.KitError("--wait-seconds must be between 0 and 1800.")
        # Termux sshd can provide a minimal PATH; use its absolute Python path.
        remote = ('exec "${PREFIX:-/data/data/com.termux/files/usr}/bin/python" -c '
                  + shlex.quote(REMOTE_HELPER) + " " + action + " " + str(wait_seconds))
        try:
            result = subprocess.run(self.argv() + [remote], input=payload or b"",
                                    capture_output=True, timeout=wait_seconds + 360)
        except FileNotFoundError as error:
            raise cerro.KitError("OpenSSH client not found; install it or pass --ssh.") from error
        except subprocess.TimeoutExpired as error:
            raise cerro.KitError("SSH operation timed out; check phone status before retrying deploy.") from error
        if result.returncode not in (0, 3):
            # Print only our own controlled error, never SSH banners, credentials,
            # raw traceback, device output or server logs from a remote endpoint.
            lines = result.stderr.decode("utf-8", errors="replace").splitlines()
            detail = next((line[7:] for line in lines if line.startswith("Error: ")), "")
            if detail:
                raise cerro.KitError(detail[:1000])
            raise cerro.KitError("SSH connection/command failed; check VPN reachability, pinned host key, deployment key and Termux Python.")
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if output:
            print(output)
        return result.returncode


def payload_archive(bundle: Path) -> bytes:
    bundle = bundle.expanduser().absolute()
    checksums = cerro.verify_bundle(bundle)
    if "cerro.py" not in checksums:
        raise cerro.KitError("SSH deployment requires cerro.py in the verified kit for on-device inference checks.")
    names = list(CODE_FILES)
    if "bin/llama-bench" in checksums:
        names.append("bin/llama-bench")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in names:
            source = bundle / name
            member = tarfile.TarInfo(name)
            member.size, member.mode = source.stat().st_size, 0o600
            with source.open("rb") as data:
                archive.addfile(member, data)
        manifest = "".join(f"{checksums[name]}  {name}\n" for name in names).encode("ascii")
        member = tarfile.TarInfo("SHA256SUMS")
        member.size, member.mode = len(manifest), 0o600
        archive.addfile(member, io.BytesIO(manifest))
    return output.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="phone Tailscale IP or MagicDNS name")
    parser.add_argument("--user", required=True, help="Termux username printed by whoami")
    parser.add_argument("--port", type=cerro.port, default=8022)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True, help="trusted cerro-phone Ed25519 pin from bootstrap")
    parser.add_argument("--ssh", default="ssh", help="OpenSSH executable path")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "stop", "status"):
        commands.add_parser(name)
    for name in ("deploy", "start"):
        command = commands.add_parser(name)
        command.add_argument("--wait-seconds", type=int, default=180)
        if name == "deploy":
            command.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        # Malformed or corrupted bundles fail before starting any SSH session.
        payload = payload_archive(args.bundle) if args.command == "deploy" else None
        ssh = Ssh(args.host, args.user, args.ssh_key, args.known_hosts, args.port, args.ssh)
        return ssh.run(args.command, payload=payload, wait_seconds=getattr(args, "wait_seconds", 180))
    except (cerro.KitError, OSError, UnicodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
