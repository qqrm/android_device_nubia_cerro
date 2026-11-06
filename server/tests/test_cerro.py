"""Host-side behavior tests; these do not claim Android device validation."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "cerro.py"
LAUNCHER = SCRIPT.parent / "device/run-server.sh"
SPEC = importlib.util.spec_from_file_location("cerro", SCRIPT)
cerro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cerro)
KEY = "a" * 64


def android_elf():
    interpreter = b"/system/bin/linker64\x00"
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", header, 16, 3, 183)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HHH", header, 52, 64, 56, 1)
    program = bytearray(56)
    struct.pack_into("<I", program, 0, 3)
    struct.pack_into("<Q", program, 8, 120)
    struct.pack_into("<Q", program, 32, len(interpreter))
    return header + program + interpreter


def manifest(bundle):
    files = sorted(path for path in bundle.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (bundle / "SHA256SUMS").write_text("".join(
        f"{cerro.digest_file(path)}  {path.relative_to(bundle).as_posix()}\n" for path in files), encoding="utf-8")


def make_bundle(directory):
    (directory / "bin").mkdir()
    (directory / "device").mkdir()
    (directory / "bin/llama-server").write_bytes(android_elf())
    (directory / "device/run-server.sh").write_text("#!/system/bin/sh\n", encoding="utf-8")
    data = {"schema_version": 1, "abi": "arm64-v8a", "android_api": 28,
            "backend": "cpu", "llama_repository": "ggml-org/llama.cpp",
            "kit_commit": "a" * 40, "llama_commit": "b" * 40, "ndk_version": "29.0.14206865"}
    (directory / "provenance.json").write_text(json.dumps(data), encoding="utf-8")
    manifest(directory)


def result(stdout=b"", stderr=b"", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.temp.name)
        make_bundle(self.bundle)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_bundle_and_local_verify_without_adb(self):
        with mock.patch.object(cerro.subprocess, "run") as adb, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cerro.main(["verify", "--bundle", str(self.bundle)]), 0)
        adb.assert_not_called()

    def test_corruption_and_extra_files_are_rejected(self):
        (self.bundle / "bin/llama-server").write_bytes(b"bad")
        with self.assertRaisesRegex(cerro.KitError, "Checksum"):
            cerro.verify_bundle(self.bundle)
        (self.bundle / "unexpected").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(cerro.KitError, "file list"):
            cerro.verify_bundle(self.bundle)

    def test_manifest_paths_cannot_escape_on_posix_or_windows(self):
        for name in ("../outside", "/absolute", "a/../outside", "a//file", "a\\file", "C:outside", "./file"):
            with self.subTest(name=name):
                (self.bundle / "SHA256SUMS").write_text("0" * 64 + "  " + name + "\n", encoding="utf-8")
                with self.assertRaisesRegex(cerro.KitError, "Unsafe"):
                    cerro.verify_bundle(self.bundle)

    def test_symlink_payload_is_rejected(self):
        (self.bundle / "link").symlink_to(self.bundle / "bin")
        with self.assertRaisesRegex(cerro.KitError, "symlink"):
            cerro.verify_bundle(self.bundle)

    def test_duplicate_manifest_entry_is_rejected(self):
        sums = self.bundle / "SHA256SUMS"
        content = sums.read_text()
        sums.write_text(content + content.splitlines()[0] + "\n")
        with self.assertRaisesRegex(cerro.KitError, "Duplicate"):
            cerro.verify_bundle(self.bundle)

    def test_hashes_alone_do_not_accept_host_binary(self):
        binary = self.bundle / "bin/llama-server"
        data = bytearray(binary.read_bytes())
        struct.pack_into("<H", data, 18, 62)  # x86-64
        binary.write_bytes(data)
        manifest(self.bundle)
        with self.assertRaisesRegex(cerro.KitError, "AArch64"):
            cerro.verify_bundle(self.bundle)

    def test_hashes_alone_do_not_accept_linux_arm64_binary(self):
        binary = self.bundle / "bin/llama-server"
        binary.write_bytes(binary.read_bytes().replace(b"/system/bin/linker64", b"/usr/xx/bin/linker64"))
        manifest(self.bundle)
        with self.assertRaisesRegex(cerro.KitError, "Android.*linker"):
            cerro.verify_bundle(self.bundle)

    def test_target_provenance_is_required(self):
        provenance = self.bundle / "provenance.json"
        data = json.loads(provenance.read_text())
        data["backend"] = "cuda"
        provenance.write_text(json.dumps(data))
        manifest(self.bundle)
        with self.assertRaisesRegex(cerro.KitError, "Provenance"):
            cerro.verify_bundle(self.bundle)


class AdbTests(unittest.TestCase):
    def test_wrong_device_is_refused_before_any_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "bundle"
            bundle.mkdir()
            make_bundle(bundle)
            model = Path(folder) / "model with 'quotes';.gguf"
            model.write_bytes(b"GGUFexample")
            key = Path(folder) / "not-created-key"
            calls = [result(b"List of devices attached\nprivate-id\tdevice\n"),
                     result(b"Another phone\nother\narm64-v8a\n35\n")]
            with mock.patch.object(cerro.subprocess, "run", side_effect=calls) as run:
                with self.assertRaisesRegex(cerro.KitError, "Refusing"):
                    cerro.deploy(cerro.Adb(), bundle, model, key)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(key.exists())
            self.assertTrue(all("push" not in call.args[0] for call in run.call_args_list))

    def test_ambiguous_devices_need_explicit_selection(self):
        with mock.patch.object(cerro.subprocess, "run", return_value=result(b"List of devices attached\na\tdevice\nb\tdevice\n")):
            with self.assertRaisesRegex(cerro.KitError, "exactly one"):
                cerro.Adb().select()

    def test_valid_model_device_and_abi(self):
        for properties in (b"NX721J\nstock-name\narm64-v8a,armeabi-v7a\n35\n",
                           b"LineageOS\ncerro\narm64-v8a\n36\n"):
            with self.subTest(properties=properties):
                with mock.patch.object(cerro.subprocess, "run", side_effect=[result(b"List of devices attached\nid\tdevice\n"), result(properties)]):
                    adb = cerro.Adb()
                    adb.select()
                    self.assertEqual(adb.serial, "id")

    def test_input_is_canonical_bytes_not_windows_text_pipe(self):
        with mock.patch.object(cerro.subprocess, "run", return_value=result(b"done\n")) as run:
            response = cerro.Adb(serial="private-id").shell("cat > fixed-path", input_text=KEY + "\n")
        self.assertEqual(run.call_args.kwargs["input"], (KEY + "\n").encode("ascii"))
        self.assertNotIn("text", run.call_args.kwargs)
        self.assertEqual(response.stdout, "done\n")
        self.assertNotIn(KEY, repr(run.call_args.args))

    def test_local_file_paths_are_single_subprocess_arguments(self):
        special = "C:/a folder/$(touch nope); model.gguf"
        with mock.patch.object(cerro.subprocess, "run", return_value=result()) as run:
            cerro.Adb(serial="private-id").run("push", special, cerro.REMOTE + "/model.gguf")
        self.assertEqual(run.call_args.args[0][4], special)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_adb_error_redacts_selected_identifier(self):
        with mock.patch.object(cerro.subprocess, "run", return_value=result(stderr=b"device private-id unavailable", code=1)):
            with self.assertRaises(cerro.KitError) as error:
                cerro.Adb(serial="private-id").shell("true")
        self.assertNotIn("private-id", str(error.exception))


class KeyTests(unittest.TestCase):
    def test_key_generation_permissions_and_lf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "credentials/api-key"
            first = cerro.read_key(path, create=True)
            self.assertEqual(cerro.read_key(path, create=True), first)
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            self.assertEqual(path.read_bytes(), first.encode("ascii") + b"\n")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_empty_or_weak_key_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api-key"
            for value in ("", "password", "a" * 63, "A" * 64):
                path.write_text(value)
                path.chmod(0o600)
                with self.assertRaises(cerro.KitError):
                    cerro.read_key(path)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Path(self.temp.name) / "api-key"
        self.key.write_bytes(KEY.encode() + b"\n")
        self.key.chmod(0o600)
        owner = self
        self.mode = "normal"
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/credential-recipient")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                authorized = self.headers.get("Authorization") == "Bearer " + KEY
                code = 200 if authorized or owner.mode == "no-auth" else 401
                if owner.mode == "wrong-key" and authorized:
                    code = 401
                self.send_response(code)
                self.end_headers()
                content = b'{"content":"hello"}' if owner.mode != "empty" else b'{"content":""}'
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_real_http_probe_checks_auth_and_inference(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cerro.probe(self.port, self.key, 0)
        self.assertIn("PASS", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        self.assertIn(("/completion", None), self.requests)
        self.assertIn(("/completion", "Bearer " + KEY), self.requests)

    def test_missing_authentication_is_a_failure(self):
        self.mode = "no-auth"
        with self.assertRaisesRegex(cerro.KitError, "Authentication check"):
            cerro.probe(self.port, self.key, 0)

    def test_wrong_key_and_empty_generation_fail(self):
        for mode in ("wrong-key", "empty"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(cerro.KitError):
                    cerro.probe(self.port, self.key, 0)

    def test_redirect_does_not_forward_credentials(self):
        code, _ = cerro.request(f"http://127.0.0.1:{self.port}", "/redirect", KEY)
        self.assertEqual(code, 302)
        self.assertFalse(any(path == "/credential-recipient" for path, _ in self.requests))


@unittest.skipUnless(Path("/proc/self/stat").exists() and shutil.which("sh") and shutil.which("sleep"), "Linux process identity tests")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "server with spaces"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "models").mkdir()
        (self.root / "bin/llama-server").symlink_to(shutil.which("sleep"))
        (self.root / "models/model.gguf").write_bytes(b"GGUFexample")
        (self.root / "config/api-key").write_bytes(KEY.encode() + b"\n")
        self.child = subprocess.Popen([shutil.which("sleep"), "30"])
        self.environment = {**os.environ, "CERRO_SERVER_DIR": str(self.root)}

    def tearDown(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.wait(timeout=5)
        self.temp.cleanup()

    def run_control(self, action):
        return subprocess.run(["sh", str(LAUNCHER), action], env=self.environment, capture_output=True, text=True, timeout=10)

    def write_pid(self, offset=0):
        try:
            stat = Path(f"/proc/{self.child.pid}/stat").read_text()
        except OSError:
            self.skipTest("This executor does not expose child processes in /proc; covered on Linux CI.")
        ticks = stat.rpartition(") ")[2].split()[19]
        (self.root / "server.pid").write_text(f"{self.child.pid} {int(ticks) + offset}\n")

    def test_recycled_pid_is_not_killed(self):
        self.write_pid(offset=1)
        self.assertEqual(self.run_control("stop").returncode, 0)
        self.assertIsNone(self.child.poll())

    def test_other_executable_is_not_killed(self):
        self.write_pid()
        (self.root / "bin/llama-server").unlink()
        (self.root / "bin/llama-server").write_text("different executable")
        self.assertEqual(self.run_control("stop").returncode, 0)
        self.assertIsNone(self.child.poll())

    def test_verified_process_can_be_stopped(self):
        self.write_pid()
        self.assertEqual(self.run_control("status").returncode, 0)
        stopped = self.run_control("stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.child.wait(timeout=5)
        self.assertEqual(self.run_control("status").returncode, 3)

    def test_empty_key_fails_closed_before_launch(self):
        (self.root / "config/api-key").write_text("")
        started = self.run_control("start")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("API key", started.stderr)
        self.assertFalse((self.root / "server.pid").exists())

    def test_interrupted_install_blocks_start(self):
        (self.root / ".deploying").touch()
        started = self.run_control("start")
        self.assertNotEqual(started.returncode, 0)
        self.assertIn("interrupted installation", started.stderr)


if __name__ == "__main__":
    unittest.main()
