#!/usr/bin/env python3
"""Deploy the Cerro native server kit using an already-authorized ADB connection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request


REMOTE = "/data/local/tmp/cerro-server"
SERVER_PORT = 8080
PAYLOAD = ("bin/llama-server", "device/run-server.sh", "provenance.json")
KEY_PATTERN = re.compile(r"[0-9a-f]{64}")


class KitError(Exception):
    """An actionable error which contains no credentials or device identifiers."""


def default_key_file() -> Path:
    base = Path(os.environ["APPDATA"]) if os.name == "nt" and os.environ.get("APPDATA") else Path.home() / ".config"
    return base / "cerro-server" / "api-key"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_android_binary(path: Path) -> None:
    """Reject host executables and wrong-architecture artifacts without invoking them."""
    with path.open("rb") as source:
        header = source.read(64)
        if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
                or struct.unpack_from("<HH", header, 16) != (3, 183)):
            raise KitError(f"{path.name} is not an AArch64 little-endian PIE executable.")
        offset = struct.unpack_from("<Q", header, 32)[0]
        entry_size, count = struct.unpack_from("<HH", header, 54)
        if entry_size != 56 or not 1 <= count <= 128 or offset < 64 or offset + count * 56 > path.stat().st_size:
            raise KitError(f"{path.name} has an invalid ELF program-header table.")
        interpreter = None
        for index in range(count):
            source.seek(offset + index * 56)
            program = source.read(56)
            if struct.unpack_from("<I", program)[0] == 3:  # PT_INTERP
                start = struct.unpack_from("<Q", program, 8)[0]
                length = struct.unpack_from("<Q", program, 32)[0]
                if length > 256 or start + length > path.stat().st_size:
                    raise KitError(f"{path.name} has an invalid ELF interpreter.")
                source.seek(start)
                interpreter = source.read(length)
        if interpreter != b"/system/bin/linker64\x00":
            raise KitError(f"{path.name} does not use the Android 64-bit linker.")


def verify_provenance(path: Path) -> None:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise KitError("Invalid provenance.json.") from error
    required = {"schema_version": 1, "abi": "arm64-v8a", "android_api": 28,
                "backend": "cpu", "llama_repository": "ggml-org/llama.cpp"}
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in required.items()):
        raise KitError("Provenance does not describe the supported Android API 28 / ARM64 CPU kit.")
    if any(not re.fullmatch(r"[0-9a-f]{40}", str(metadata.get(key, ""))) for key in ("kit_commit", "llama_commit")):
        raise KitError("Provenance must pin the kit and upstream to full commit SHAs.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(metadata.get("ndk_version", ""))):
        raise KitError("Provenance lacks a pinned Android NDK version.")


def verify_bundle(directory: Path) -> dict[str, str]:
    """Check the entire extracted bundle, including path safety, before ADB writes."""
    directory = directory.expanduser().absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise KitError("Bundle must be an extracted directory, not a symlink or archive.")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise KitError("Bundle contains a symlink; extract a fresh trusted artifact.")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise KitError("Bundle contains an unsupported file type.")
    manifest = directory / "SHA256SUMS"
    if "SHA256SUMS" not in actual:
        raise KitError("Bundle is missing SHA256SUMS.")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise KitError("Invalid SHA256SUMS entry.")
        checksum, name = match.groups()
        parts = name.split("/")
        if (PurePosixPath(name).is_absolute() or any(part in ("", ".", "..") for part in parts)
                or "\\" in name or ":" in name or "\x00" in name or name == "SHA256SUMS"):
            raise KitError("Unsafe path in SHA256SUMS.")
        if name in expected:
            raise KitError("Duplicate path in SHA256SUMS.")
        expected[name] = checksum.lower()
    if set(expected) != actual - {"SHA256SUMS"}:
        raise KitError("Bundle file list does not match SHA256SUMS.")
    if not set(PAYLOAD).issubset(expected):
        raise KitError("Bundle lacks the server binary, launcher, or provenance.json.")
    for name, checksum in expected.items():
        if digest_file(directory.joinpath(*name.split("/"))) != checksum:
            raise KitError(f"Checksum mismatch: {name}")
    verify_provenance(directory / "provenance.json")
    for name in ("bin/llama-server", "bin/llama-bench"):
        if name in expected:
            verify_android_binary(directory / name)
    return expected


def read_key(path: Path, *, create: bool = False) -> str:
    path = path.expanduser()
    if path.is_symlink():
        raise KitError("The API key must be a regular file, not a symlink.")
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as target:
                target.write(secrets.token_hex(32) + "\n")
    if not path.is_file():
        raise KitError("API key file is missing. Deploy first or pass --key-file.")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise KitError("API key file permissions are too broad; use chmod 600 on that file.")
    key = path.read_text(encoding="ascii").strip()
    if not KEY_PATTERN.fullmatch(key):
        raise KitError("API key must contain exactly 64 lowercase hexadecimal characters.")
    return key


class Adb:
    def __init__(self, executable: str = "adb", serial: str | None = None):
        self.executable = executable
        self.serial = serial

    def run(self, *args: str, input_text: str | None = None,
            allowed: tuple[int, ...] = (0,), device: bool = True) -> subprocess.CompletedProcess:
        command = [self.executable]
        if device:
            if not self.serial:
                raise KitError("No ADB device has been selected.")
            command.extend(("-s", self.serial))
        command.extend(args)
        try:
            # Binary pipes preserve LF on Windows when uploading keys and manifests.
            data = input_text.encode("utf-8") if input_text is not None else None
            result = subprocess.run(command, input=data, capture_output=True, check=False)
        except FileNotFoundError as error:
            raise KitError("ADB was not found. Install Android SDK Platform-Tools or pass --adb.") from error
        result.stdout = result.stdout.decode("utf-8", errors="replace")
        result.stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode not in allowed:
            detail = (result.stderr or result.stdout).strip()
            if self.serial:
                detail = detail.replace(self.serial, "<device>")
            # Never include command arguments or stdin: they can contain private values.
            raise KitError(f"ADB operation failed: {detail[:1500] or 'no diagnostic returned'}")
        return result

    def shell(self, command: str, **kwargs) -> subprocess.CompletedProcess:
        return self.run("shell", command, **kwargs)

    def select(self) -> None:
        result = self.run("devices", device=False)
        devices = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] in ("device", "offline", "unauthorized", "recovery", "sideload"):
                devices.append((fields[0], fields[1]))
        if self.serial:
            candidates = [item for item in devices if item[0] == self.serial]
            if len(candidates) != 1 or candidates[0][1] != "device":
                raise KitError("The selected ADB device is unavailable or not authorized in Android.")
        else:
            if len(devices) != 1 or devices[0][1] != "device":
                raise KitError("Connect and authorize exactly one Android device, or select it with --serial.")
            self.serial = devices[0][0]
        properties = self.shell("getprop ro.product.model; getprop ro.product.device; getprop ro.product.cpu.abilist; getprop ro.build.version.sdk").stdout.splitlines()
        if len(properties) != 4:
            raise KitError("Could not verify the Android device properties.")
        model, device, abis, sdk = (value.strip() for value in properties)
        if model != "NX721J" and device not in ("cerro", "PQ83A01"):
            raise KitError("Refusing this device: expected Nubia NX721J / cerro / PQ83A01.")
        if "arm64-v8a" not in abis.split(",") or not sdk.isdigit() or int(sdk) < 28:
            raise KitError("The kit requires arm64-v8a Android API 28 or newer.")


def deploy(adb: Adb, bundle: Path, model: Path, key_file: Path) -> None:
    bundle, model = bundle.expanduser().absolute(), model.expanduser().absolute()
    checksums = verify_bundle(bundle)
    if not model.is_file():
        raise KitError("Model is missing; provide a local GGUF file with --model.")
    with model.open("rb") as source:
        if source.read(4) != b"GGUF":
            raise KitError("Model does not have a GGUF header.")
    adb.select()
    key = read_key(key_file, create=True)
    stage = REMOTE + "/.stage-" + secrets.token_hex(16)
    paths = list(PAYLOAD)
    if "bin/llama-bench" in checksums:
        paths.append("bin/llama-bench")
    adb.shell(f"umask 077; [ ! -L {shlex.quote(REMOTE)} ] && mkdir -p {shlex.quote(REMOTE)} && chmod 700 {shlex.quote(REMOTE)} && mkdir {shlex.quote(stage)} && mkdir {stage}/bin {stage}/device {stage}/models {stage}/config")
    try:
        for name in paths:
            adb.run("push", str(bundle.joinpath(*name.split("/"))), stage + "/" + name)
        print("Transferring the model; large GGUF files can take several minutes.")
        adb.run("push", str(model), stage + "/models/model.gguf")
        adb.shell(f"umask 077; cat > {stage}/config/api-key", input_text=key + "\n")
        transferred = "".join(f"{checksums[name]}  {name}\n" for name in paths)
        transferred += f"{digest_file(model)}  models/model.gguf\n"
        adb.shell(f"umask 077; cat > {stage}/upload.sha256", input_text=transferred)
        adb.shell(f"cd {stage} && sha256sum -c upload.sha256 >/dev/null")
        result = adb.shell(f"CERRO_SERVER_DIR={REMOTE} sh {stage}/device/run-server.sh install {stage}")
        print(result.stdout.strip())
    finally:
        # This path was generated here, contains only hexadecimal entropy, and is inside our directory.
        try:
            adb.shell(f"rm -rf {stage}")
        except KitError:
            pass
    print(f"API key saved to {key_file.expanduser()}; its value is not printed.")


def control(adb: Adb, action: str) -> int:
    adb.select()
    launcher = REMOTE + "/device/run-server.sh"
    result = adb.shell(f"if [ -f {launcher} ]; then CERRO_SERVER_DIR={REMOTE} sh {launcher} {action}; else echo 'Kit is not deployed.'; exit 3; fi", allowed=(0, 3))
    print(result.stdout.strip())
    return result.returncode


def forward(adb: Adb, local_port: int) -> None:
    adb.select()
    result = adb.run("forward", "--no-rebind", f"tcp:{local_port}", f"tcp:{SERVER_PORT}", allowed=(0, 1))
    if result.returncode:
        existing = [line.split() for line in adb.run("forward", "--list").stdout.splitlines()]
        if [adb.serial, f"tcp:{local_port}", f"tcp:{SERVER_PORT}"] not in existing:
            raise KitError("Cannot create the forward; the local port may already be in use.")
    print(f"Forward ready at http://127.0.0.1:{local_port}; requests require the API key.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def request(base: str, endpoint: str, key: str | None = None, payload: dict | None = None,
            timeout: float = 120.0) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + endpoint, data=data, headers=headers)
    # Local requests must never be sent through a proxy configured in the host environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(1024 * 1024)


def probe(local_port: int, key_file: Path, wait_seconds: int) -> None:
    key = read_key(key_file)
    base = f"http://127.0.0.1:{local_port}"
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            code, _ = request(base, "/health", key, timeout=5)
            if code == 200:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if time.monotonic() >= deadline:
            raise KitError("Server is not ready; check start/status and the ADB forward or SSH tunnel.")
        time.sleep(1)
    payload = {"prompt": "Reply with one short word: hello", "n_predict": 8, "temperature": 0, "stream": False}
    code, _ = request(base, "/completion", payload=payload, timeout=15)
    if code not in (401, 403):
        raise KitError("Authentication check failed: an unauthenticated completion was not rejected.")
    code, body = request(base, "/completion", key, payload)
    if code != 200:
        raise KitError(f"Authenticated inference failed with HTTP {code}.")
    try:
        result = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KitError("Authenticated inference returned invalid JSON.") from error
    if not isinstance(result, dict) or not isinstance(result.get("content"), str) or not result["content"].strip():
        raise KitError("Authenticated inference returned no generated text.")
    print("PASS: ready; unauthenticated inference rejected; authenticated inference generated text.")


def port(value: str) -> int:
    result = int(value)
    if not 1 <= result <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb", help="ADB executable path")
    parser.add_argument("--serial", help="select one already-authorized ADB device")
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="verify bundle checksums, target and Android executables locally")
    verify.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    install = commands.add_parser("deploy", help="verify and copy the kit and a local GGUF model")
    install.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    install.add_argument("--model", type=Path, required=True)
    install.add_argument("--key-file", type=Path, default=default_key_file())
    for action in ("start", "stop", "status"):
        commands.add_parser(action)
    tunnel = commands.add_parser("forward", help="forward a host loopback port using ADB")
    tunnel.add_argument("--local-port", type=port, default=SERVER_PORT)
    check = commands.add_parser("probe", help="test local API readiness, authentication and inference")
    check.add_argument("--local-port", type=port, default=SERVER_PORT)
    check.add_argument("--key-file", type=Path, default=default_key_file())
    check.add_argument("--wait-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    try:
        adb = Adb(args.adb, args.serial)
        if args.command == "verify":
            verify_bundle(args.bundle)
            print("PASS: bundle checksums, provenance and Android ARM64 ELF headers verified.")
        elif args.command == "deploy":
            deploy(adb, args.bundle, args.model, args.key_file)
        elif args.command == "forward":
            forward(adb, args.local_port)
        elif args.command == "probe":
            if args.wait_seconds < 0:
                raise KitError("--wait-seconds cannot be negative.")
            probe(args.local_port, args.key_file, args.wait_seconds)
        else:
            return control(adb, args.command)
    except (KitError, OSError, UnicodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
