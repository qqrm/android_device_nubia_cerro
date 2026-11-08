#!/usr/bin/env python3
"""Run native SSH and a tiny CPU inference test in an existing stock Cuttlefish VM.

This does not test a no-Zygote image or any phone hardware. It uses only the
unprivileged Android shell account, a fresh /data/local/tmp directory and two
loopback ADB forwards. Credentials are temporary and excluded from reports.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request


GUEST_ROOT = "/data/local/tmp/cerro-native"
ADB = "/usr/lib/cuttlefish-common/bin/adb"
BINARIES = ("llama-server", "sshd", "sshd-session", "sshd-auth")
SSH_PORT, HTTP_PORT = 2222, 18080
COMPLETION = {"prompt": "Once upon a time", "n_predict": 8,
              "seed": 1, "cache_prompt": False}


class SmokeError(RuntimeError):
    pass


def validate_serial(value):
    # Never let adb choose a default device, or accept a USB/phone identifier.
    match = re.fullmatch(r"(127\.0\.0\.1|localhost|0\.0\.0\.0):([0-9]{1,5})", value)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ValueError("ADB serial must be an explicit Cuttlefish loopback host:port")
    return value


def parse_http(value):
    body, separator, status = value.rpartition("\n")
    if not separator or not re.fullmatch(r"[1-5][0-9]{2}", status):
        raise SmokeError("curl did not return a valid HTTP status trailer")
    return int(status), body


def validate_completion(value):
    if not isinstance(value, dict) or not isinstance(value.get("content"), str):
        raise SmokeError("Inference response has no string content")
    count = value.get("tokens_predicted")
    if type(count) is not int or count <= 0:
        raise SmokeError("Inference response did not generate any tokens")
    return count


def validate_guest(value):
    if value["properties"]["ro.product.device"] != "vsoc_x86_64_only":
        raise SmokeError("Refusing a guest other than the expected x86_64 Cuttlefish product")
    if value["properties"]["ro.product.cpu.abi"] != "x86_64":
        raise SmokeError("Expected an x86_64 Android guest")
    if value["uid"] != "2000" or value["context"] != "u:r:shell:s0":
        raise SmokeError("The native test requires unprivileged uid 2000 in the shell domain")
    if value["selinux"] not in ("Enforcing", "Permissive"):
        raise SmokeError("Could not read the guest SELinux enforcement mode")
    if not {"zygote64", "system_server"} <= set(value["framework_processes_observed"]):
        raise SmokeError("Expected the stock Android framework to remain running")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_bundle(bundle):
    """Check transport integrity and the separate x86_64 bundle's pinned identity."""
    shared = json.loads((Path(__file__).resolve().parents[1] / "versions.json").read_text())
    pins = {**shared, **json.loads(Path(__file__).with_name("versions.json").read_text())}
    if not bundle.is_dir() or any(path.is_symlink() for path in bundle.rglob("*")):
        raise SmokeError("Bundle must be a directory without symlinks")
    listed = {}
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise SmokeError("Invalid SHA256SUMS entry")
        relative = PurePosixPath(match[2])
        if (relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != match[2] or match[2] in listed):
            raise SmokeError("Unsafe or duplicate SHA256SUMS path")
        listed[match[2]] = match[1]
    actual = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*")
              if path.is_file() and path != bundle / "SHA256SUMS"}
    if set(listed) != actual:
        raise SmokeError("Bundle file inventory differs from SHA256SUMS")
    required = {"provenance.json", *(f"bin/{name}" for name in BINARIES)}
    if not required <= actual:
        raise SmokeError("Bundle is missing a required native binary or provenance")
    for relative, expected in listed.items():
        if sha256(bundle / relative) != expected:
            raise SmokeError(f"Bundle checksum mismatch: {relative}")
    provenance = json.loads((bundle / "provenance.json").read_text())
    if any(provenance.get(key) != value for key, value in pins.items()):
        raise SmokeError("Bundle provenance does not match the pinned virtual versions")
    if (provenance.get("guest_root") != GUEST_ROOT
            or provenance.get("artifact") != "cerro-native-android-x86_64"
            or provenance.get("validation", {}).get("elf_verified") is not True):
        raise SmokeError("Not the verified isolated Cuttlefish native bundle")
    for name in BINARIES:
        with (bundle / "bin" / name).open("rb") as binary:
            header = binary.read(20)
        if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\x3e\x00":
            raise SmokeError(f"{name} is not a little-endian x86_64 ELF64 binary")
    return provenance, pins


def download_model(pins, destination):
    if (pins["model_repository"] != "ggml-org/models"
            or pins["model_file"] != "tinyllamas/stories260K.gguf"
            or not re.fullmatch(r"[0-9a-f]{40}", pins["model_revision"])
            or not re.fullmatch(r"[0-9a-f]{64}", pins["model_sha256"])
            or type(pins["model_bytes"]) is not int or pins["model_bytes"] <= 0):
        raise SmokeError("Invalid pinned test model")
    url = (f"https://huggingface.co/{pins['model_repository']}/resolve/"
           f"{pins['model_revision']}/{pins['model_file']}")
    count, started = 0, time.monotonic()
    with urllib.request.urlopen(url, timeout=30) as response, destination.open("wb") as target:
        while chunk := response.read(65536):
            count += len(chunk)
            if count > pins["model_bytes"] or time.monotonic() - started > 120:
                raise SmokeError("Pinned model download exceeded its size or time limit")
            target.write(chunk)
    if count != pins["model_bytes"] or sha256(destination) != pins["model_sha256"]:
        raise SmokeError("Pinned model download failed size/SHA256 verification")


class Smoke:
    def __init__(self, args, temporary):
        self.args, self.temporary = args, temporary
        self.token = secrets.token_hex(16)
        self.api_key = secrets.token_hex(32)
        self.controller = f"/tmp/cerro-native-{self.token}"
        self.guest_created = self.controller_created = False
        self.forwards, self.processes = [], {}
        self.report = {
            "schema_version": 1, "status": "failed",
            "scope": "native processes on stock Android Cuttlefish",
            "no_zygote_verified": False, "phone_tested": False,
            "limitations": ["The stock Android framework remains running.",
                            "No Cerro image, vendor HAL, Wi-Fi, charging or thermal hardware is tested.",
                            "The tiny CPU model checks execution, not inference quality or performance."],
            "tests": {}, "cleanup": {},
        }

    def clean_text(self, value):
        value = value.replace(self.api_key, "[redacted]")
        return re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                      "[redacted private key]", value, flags=re.DOTALL)

    def command(self, label, argv, *, input=None, timeout=30, check=True):
        try:
            result = subprocess.run([str(arg) for arg in argv], input=input, text=True,
                                    encoding="utf-8", errors="replace", capture_output=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise SmokeError(f"{label} exceeded {timeout} seconds") from error
        if check and result.returncode:
            details = self.clean_text(result.stderr.strip())[-2000:]
            raise SmokeError(f"{label} exited {result.returncode}: {details}")
        return result

    def docker(self, *argv, **kwargs):
        flags = ["-i"] if kwargs.get("input") is not None else []
        return self.command("container command", ["docker", "exec", *flags,
                            self.args.container, *argv], **kwargs)

    def adb(self, *argv, **kwargs):
        return self.docker(ADB, "-s", self.args.serial, *argv, **kwargs)

    def shell(self, script, **kwargs):
        return self.adb("shell", script, **kwargs)

    def inventory(self):
        properties = ("ro.product.device", "ro.product.cpu.abi", "ro.build.version.sdk",
                      "ro.build.fingerprint", "ro.zygote")
        value = {
            "id": self.shell("id").stdout.strip(),
            "uid": self.shell("id -u").stdout.strip(),
            "context": self.shell("id -Z").stdout.strip(),
            "selinux": self.shell("getenforce").stdout.strip(),
            "properties": {name: self.shell(f"getprop {name}").stdout.strip()
                           for name in properties},
            "processes_pid_name": self.shell("ps -A -o PID,NAME").stdout.splitlines(),
        }
        names = {line.split()[-1] for line in value["processes_pid_name"] if line.split()}
        value["framework_processes_observed"] = sorted(names & {"zygote", "zygote64", "system_server"})
        return value

    def prepare(self, provenance, pins):
        guest = self.temporary / "guest"
        client = self.temporary / "controller-client"
        (guest / "bin").mkdir(parents=True)
        client.mkdir()
        for name in BINARIES:
            shutil.copy2(self.args.bundle / "bin" / name, guest / "bin" / name)
        download_model(pins, guest / "stories260K.gguf")
        for name in ("ca", "client", "untrusted", "host-key"):
            self.command("ephemeral key generation", ["ssh-keygen", "-q", "-t", "ed25519",
                         "-N", "", "-f", self.temporary / name])
        self.command("ephemeral client certificate", ["ssh-keygen", "-q", "-s", self.temporary / "ca",
                     "-I", "cerro-cuttlefish-test", "-n", "shell", "-V", "-5m:+1h",
                     self.temporary / "client.pub"])
        # Signing private key stays on the host. Server sees only its public CA.
        for name in ("ca.pub", "host-key"):
            shutil.copy2(self.temporary / name, guest / name)
        for name in ("client", "client-cert.pub", "untrusted"):
            shutil.copy2(self.temporary / name, client / name)
        host_public = " ".join((self.temporary / "host-key.pub").read_text().split()[:2])
        (client / "known_hosts").write_text(f"[127.0.0.1]:{SSH_PORT} {host_public}\n")
        (client / "curl.conf").write_text(f'header = "Authorization: Bearer {self.api_key}"\n')
        (guest / "api-key").write_text(self.api_key + "\n")
        (guest / "sshd_config").write_text("\n".join([
            "Port 2222", "ListenAddress 127.0.0.1", f"HostKey {GUEST_ROOT}/host-key",
            f"PidFile {GUEST_ROOT}/sshd.pid", "AllowUsers shell", "StrictModes yes",
            "AuthenticationMethods publickey", "PubkeyAuthentication yes",
            "PasswordAuthentication no", "KbdInteractiveAuthentication no", "UsePAM no",
            "AuthorizedKeysFile none", f"TrustedUserCAKeys {GUEST_ROOT}/ca.pub",
            "DisableForwarding yes", "PermitTTY no", "PermitUserRC no", "UseDNS no",
            "LoginGraceTime 20", "LogLevel VERBOSE", "",
        ]))
        for directory in (guest, client):
            for path in directory.iterdir():
                if path.is_file():
                    path.chmod(0o600)
        self.docker("mkdir", "-m", "700", self.controller)
        self.controller_created = True
        for name, source in (("client", client), ("guest", guest)):
            self.command("copy temporary runtime files", ["docker", "cp", source,
                         f"{self.args.container}:{self.controller}/{name}"], timeout=120)
        root = shlex.quote(GUEST_ROOT)
        self.shell(f"[ ! -e {root} ] && [ ! -L {root} ] && "
                   f"(umask 077; mkdir {root} && printf '%s' {self.token} > {root}/.owner)")
        self.guest_created = True
        self.adb("push", f"{self.controller}/guest/bin", f"{GUEST_ROOT}/bin", timeout=120)
        for name in ("stories260K.gguf", "api-key", "sshd_config", "ca.pub", "host-key"):
            self.adb("push", f"{self.controller}/guest/{name}", f"{GUEST_ROOT}/{name}", timeout=120)
        self.shell(f"chmod 700 {root} {root}/bin && chmod 755 {root}/bin/* && "
                   f"chmod 600 {root}/api-key {root}/host-key {root}/sshd_config {root}/ca.pub")
        self.docker("rm", "-rf", f"{self.controller}/guest")
        self.report["model"] = {key: pins[key] for key in pins if key.startswith("model_")}
        (self.args.output_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")

    def start(self, name, argv):
        executable = f"{GUEST_ROOT}/bin/{name}"
        script = f"""
nohup {shlex.join(argv)} > {GUEST_ROOT}/{name}.log 2>&1 < /dev/null &
pid=$!
attempt=0
while [ "$attempt" -lt 50 ]; do
    kill -0 "$pid" 2>/dev/null || exit 1
    if [ "$(readlink /proc/$pid/exe 2>/dev/null)" = {shlex.quote(executable)} ]; then
        stat=$(cat /proc/$pid/stat) || exit 1
        stat=${{stat##*) }}
        set -- $stat
        [ "$#" -ge 20 ] || exit 1
        shift 19
        printf '%s %s\\n' "$pid" "$1"
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
exit 1
"""
        result = self.shell(script, timeout=15)
        match = re.fullmatch(r"([1-9][0-9]*) ([0-9]+)\s*", result.stdout)
        if not match:
            raise SmokeError(f"Could not identify the {name} process")
        self.processes[name] = (match[1], match[2], executable)

    def identity_script(self, name):
        pid, ticks, executable = self.processes[name]
        return f"""
matches() {{
    [ "$(readlink /proc/{pid}/exe 2>/dev/null)" = {shlex.quote(executable)} ] || return 1
    stat=$(cat /proc/{pid}/stat 2>/dev/null) || return 1
    stat=${{stat##*) }}
    set -- $stat
    [ "$#" -ge 20 ] || return 1
    shift 19
    [ "$1" = {ticks} ]
}}
"""

    def running(self, name):
        return self.shell(self.identity_script(name) + "matches", check=False).returncode == 0

    def forward(self, local, remote):
        self.adb("forward", "--no-rebind", f"tcp:{local}", f"tcp:{remote}")
        self.forwards.append(local)

    def ssh(self, *, authorized):
        directory = f"{self.controller}/client"
        argv = ["ssh", "-F", "/dev/null", "-n", "-T", "-p", str(SSH_PORT),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={directory}/known_hosts",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=3",
                "-o", "PreferredAuthentications=publickey", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-o", "ClearAllForwardings=yes",
                "-i", f"{directory}/{'client' if authorized else 'untrusted'}"]
        if authorized:
            argv += ["-o", f"CertificateFile={directory}/client-cert.pub"]
        return self.docker(*argv, "shell@127.0.0.1", "id -u", timeout=10, check=False)

    def test_ssh(self):
        self.shell(shlex.join([f"{GUEST_ROOT}/bin/sshd", "-t", "-f", f"{GUEST_ROOT}/sshd_config"]))
        self.start("sshd", [f"{GUEST_ROOT}/bin/sshd", "-D", "-e", "-f", f"{GUEST_ROOT}/sshd_config"])
        self.forward(SSH_PORT, 2222)
        deadline = time.monotonic() + 45
        while True:
            result = self.ssh(authorized=True)
            if result.returncode == 0:
                if result.stdout.strip() != "2000":
                    raise SmokeError("Authenticated SSH command did not run as uid 2000")
                break
            if "Permission denied" in result.stderr or not self.running("sshd") or time.monotonic() >= deadline:
                raise SmokeError("Certificate SSH login failed: " + self.clean_text(result.stderr.strip()))
            time.sleep(1)
        self.report["tests"]["ssh_certificate_command"] = {"passed": True, "uid": 2000}
        denied = self.ssh(authorized=False)
        if denied.returncode != 255 or "Permission denied (publickey)" not in denied.stderr:
            raise SmokeError("Unsigned SSH key was not rejected specifically by public-key authentication")
        self.report["tests"]["ssh_unsigned_key_rejected"] = {
            "passed": True, "client_exit_code": denied.returncode,
            "observed_error": "Permission denied (publickey)",
        }

    def http(self, path, *, authorized, body=None, max_time=10):
        argv = ["curl", "--disable", "--silent", "--show-error", "--noproxy", "*",
                "--proto", "=http", "--connect-timeout", "2", "--max-time", str(max_time),
                "--write-out", "\n%{http_code}"]
        if authorized:
            argv += ["--config", f"{self.controller}/client/curl.conf"]
        if body is not None:
            argv += ["--header", "Content-Type: application/json", "--data-binary", "@-"]
        argv += [f"http://127.0.0.1:{HTTP_PORT}{path}"]
        return self.docker(*argv, input=json.dumps(body) if body is not None else None,
                           timeout=max_time + 5, check=False)

    def test_inference(self):
        self.start("llama-server", [f"{GUEST_ROOT}/bin/llama-server",
                   "--model", f"{GUEST_ROOT}/stories260K.gguf", "--host", "127.0.0.1", "--port", "8080",
                   "--ctx-size", "128", "--threads", "2", "--parallel", "1", "--n-gpu-layers", "0",
                   "--api-key-file", f"{GUEST_ROOT}/api-key", "--offline", "--no-ui"])
        self.forward(HTTP_PORT, 8080)
        deadline = time.monotonic() + 90
        while True:
            result = self.http("/health", authorized=True, max_time=3)
            if result.returncode == 0 and parse_http(result.stdout)[0] == 200:
                break
            if not self.running("llama-server") or time.monotonic() >= deadline:
                raise SmokeError("Native inference server did not become healthy")
            time.sleep(1)
        denied = self.http("/completion", authorized=False, body=COMPLETION)
        if denied.returncode or parse_http(denied.stdout)[0] != 401:
            raise SmokeError("Inference endpoint did not reject a request without its API key with HTTP 401")
        self.report["tests"]["inference_missing_key_rejected"] = {"passed": True, "http_status": 401}
        result = self.http("/completion", authorized=True, body=COMPLETION, max_time=60)
        if result.returncode:
            raise SmokeError("Authenticated inference HTTP request failed: " + self.clean_text(result.stderr.strip()))
        status, body = parse_http(result.stdout)
        if status != 200:
            raise SmokeError(f"Authenticated inference returned HTTP {status}")
        response = json.loads(body)
        count = validate_completion(response)
        (self.args.output_dir / "inference-response.json").write_text(
            self.clean_text(json.dumps(response, indent=2)) + "\n")
        self.report["tests"]["inference_completion"] = {"passed": True, "http_status": status,
                                                       "tokens_predicted": count}

    def cleanup(self):
        errors = []
        for name in reversed(self.processes):
            pid = self.processes[name][0]
            script = self.identity_script(name) + f"""
if matches; then
    kill -TERM {pid} || exit 1
    attempt=0
    while matches && [ "$attempt" -lt 50 ]; do
        sleep 0.1
        attempt=$((attempt + 1))
    done
    matches && exit 1
fi
exit 0
"""
            try:
                self.shell(script, timeout=15)
                self.report["cleanup"][name] = "stopped, or original process already exited"
            except (SmokeError, OSError) as error:
                errors.append(str(error))
                self.report["cleanup"][name] = "could not confirm termination; VM teardown required"
        if self.guest_created:
            owner = f'[ "$(cat {GUEST_ROOT}/.owner 2>/dev/null)" = {self.token} ]'
            for name in ("sshd", "llama-server"):
                try:
                    result = self.shell(f"{owner} && "
                                        f"{{ [ ! -f {GUEST_ROOT}/{name}.log ] || tail -c 262144 {GUEST_ROOT}/{name}.log; }}")
                    (self.args.output_dir / f"{name}.log").write_text(self.clean_text(result.stdout))
                except (SmokeError, OSError) as error:
                    errors.append(str(error))
            if not errors:
                try:
                    self.shell(f"{owner} && rm -rf {GUEST_ROOT}")
                    self.report["cleanup"]["guest_directory"] = "removed"
                except (SmokeError, OSError) as error:
                    errors.append(str(error))
        for port in self.forwards:
            try:
                self.adb("forward", "--remove", f"tcp:{port}")
            except (SmokeError, OSError) as error:
                errors.append(str(error))
        if self.controller_created:
            try:
                self.docker("rm", "-rf", self.controller)
                self.report["cleanup"]["controller_credentials"] = "removed"
            except (SmokeError, OSError) as error:
                errors.append(str(error))
        if "before" in self.report:
            try:
                after = self.inventory()
                self.report["after"] = after
                validate_guest(after)
                for key in ("uid", "context", "selinux", "properties", "framework_processes_observed"):
                    if after[key] != self.report["before"][key]:
                        raise SmokeError(f"Guest {key} changed during the runtime test")
                self.report["tests"]["guest_identity_and_selinux_unchanged"] = {"passed": True}
            except (SmokeError, OSError) as error:
                errors.append(str(error))
        if errors:
            self.report["cleanup"]["errors"] = [self.clean_text(error) for error in errors]
        return not errors

    def run(self):
        started = time.monotonic()
        passed = False
        try:
            provenance, pins = verify_bundle(self.args.bundle)
            before = self.inventory()
            validate_guest(before)
            self.report["before"] = before
            self.prepare(provenance, pins)
            self.test_ssh()
            self.test_inference()
            passed = True
        except (Exception, KeyboardInterrupt) as error:
            self.report["error"] = self.clean_text(f"{type(error).__name__}: {error}")
        finally:
            cleaned = self.cleanup()
            self.report["status"] = "passed" if passed and cleaned else "failed"
            self.report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            (self.args.output_dir / "report.json").write_text(json.dumps(self.report, indent=2) + "\n")
        print(json.dumps({"status": self.report["status"], "tests": self.report["tests"],
                          "no_zygote_verified": False, "phone_tested": False,
                          "error": self.report.get("error"),
                          "cleanup_errors": self.report["cleanup"].get("errors", [])}, indent=2))
        return 0 if self.report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path, help="Extracted native x86_64 bundle")
    parser.add_argument("--container", default="cerro-cuttlefish")
    serial = parser.add_mutually_exclusive_group(required=True)
    serial.add_argument("--serial")
    serial.add_argument("--serial-file", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="New or empty report directory")
    args = parser.parse_args()
    try:
        if args.serial_file:
            args.serial = args.serial_file.read_text().strip()
        args.serial = validate_serial(args.serial)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", args.container):
            raise ValueError("Invalid container name")
        args.bundle = args.bundle.resolve()
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if any(args.output_dir.iterdir()):
            raise ValueError("Report directory must be empty; it must never contain credentials")
        temporary_base = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
        if temporary_base.is_relative_to(args.output_dir):
            raise ValueError("Credential temporary directory must be outside the report directory")
        with tempfile.TemporaryDirectory(prefix="cerro-native-", dir=temporary_base) as temporary:
            return Smoke(args, Path(temporary)).run()
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    sys.exit(main())
