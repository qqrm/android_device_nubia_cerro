"""Host/transaction tests with fixture code; these are not Android hardware tests."""

import base64
import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import phone
from test_cerro import make_bundle, manifest


KEY = "c" * 64
HOST_WIRE = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"k" * 32
HOST_LINE = "cerro-phone ssh-ed25519 " + base64.b64encode(HOST_WIRE).decode() + "\n"


def remote_namespace():
    namespace = {"__name__": "phone_remote_test"}
    exec(compile(phone.REMOTE_HELPER, "phone-remote", "exec"), namespace)
    return namespace


def make_phone_bundle(path):
    path.mkdir()
    make_bundle(path)
    (path / "cerro.py").write_text("# fixture only; inference is mocked in transaction tests\n")
    manifest(path)
    return phone.payload_archive(path)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="phone credentials ")
        self.directory = Path(self.temp.name)
        self.key = self.directory / "deploy key"
        self.key.write_text("fixture-private-key")
        self.key.chmod(0o600)
        self.pin = self.directory / "known hosts"
        self.pin.write_text(HOST_LINE)

    def tearDown(self):
        self.temp.cleanup()

    def ssh(self, **overrides):
        parameters = dict(host="100.64.10.20", user="u0_a123", key_file=self.key, known_hosts=self.pin)
        parameters.update(overrides)
        return phone.Ssh(**parameters)

    def test_ssh_has_explicit_pin_and_isolated_key_only_config(self):
        ssh = self.ssh()
        arguments = ssh.argv()
        self.assertEqual(arguments[arguments.index("-F") + 1], os.devnull)
        self.assertEqual(arguments[arguments.index("-i") + 1], str(self.key))
        for option in ("StrictHostKeyChecking=yes", "HostKeyAlias=cerro-phone",
                       "HostKeyAlgorithms=ssh-ed25519", "IdentityAgent=none", "IdentitiesOnly=yes",
                       "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                       "ForwardAgent=no", "ClearAllForwardings=yes", "UpdateHostKeys=no"):
            self.assertIn(option, arguments)
        response = subprocess.CompletedProcess([], 0, b"PASS: connection checked.\n", b"")
        with mock.patch.object(phone.subprocess, "run", return_value=response) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ssh.run("check"), 0)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["input"], b"")
        self.assertNotIn(KEY, repr(run.call_args))

    def test_host_username_and_pin_cannot_supply_ssh_options_or_shell_code(self):
        for host in ("-oProxyCommand=sh", "phone; touch anything", "user@phone", "$(hostname)", ""):
            with self.subTest(host=host), self.assertRaises(phone.cerro.KitError):
                self.ssh(host=host)
        for user in ("u0_a123;id", "$(id)", "-oProxyCommand"):
            with self.subTest(user=user), self.assertRaises(phone.cerro.KitError):
                self.ssh(user=user)
        for line in (HOST_LINE.replace("cerro-phone", "*"), HOST_LINE + HOST_LINE,
                     HOST_LINE.replace("cerro-phone", "@cert-authority cerro-phone"),
                     "cerro-phone ssh-ed25519 not-base64\n"):
            with self.subTest(pin=line):
                self.pin.write_text(line)
                with self.assertRaises(phone.cerro.KitError):
                    self.ssh()

    def test_credential_permissions_and_symlinks_fail_before_connection(self):
        if os.name != "nt":
            self.key.chmod(0o644)
            with self.assertRaisesRegex(phone.cerro.KitError, "permissions"):
                self.ssh()
            self.key.chmod(0o600)
        link = self.directory / "link"
        link.symlink_to(self.pin)
        with self.assertRaisesRegex(phone.cerro.KitError, "symlink"):
            self.ssh(known_hosts=link)

    def test_remote_diagnostics_do_not_expose_endpoint_or_raw_output(self):
        response = subprocess.CompletedProcess([], 255, KEY.encode(), b"u0_a123@100.64.10.20: Permission denied\n")
        with mock.patch.object(phone.subprocess, "run", return_value=response), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(phone.cerro.KitError) as error:
                self.ssh().run("check")
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(KEY, str(error.exception))
        self.assertNotIn("100.64.10.20", str(error.exception))
        self.assertNotIn("u0_a123", str(error.exception))

    def test_corrupt_bundle_fails_before_any_ssh_command(self):
        bundle = self.directory / "bundle"
        make_phone_bundle(bundle)
        (bundle / "bin/llama-server").write_bytes(b"corrupt")
        with mock.patch.object(phone.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            code = phone.main(["--host", "phone", "--user", "u0_a123", "--ssh-key", str(self.key),
                               "--known-hosts", str(self.pin), "deploy", "--bundle", str(bundle)])
        self.assertEqual(code, 1)
        run.assert_not_called()


class DeviceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = self.directory / "cerro-server"
        (self.root / "models").mkdir(parents=True)
        (self.root / "config").mkdir()
        self.model = self.root / "models/model.gguf"
        self.model.write_bytes(b"GGUFmodel-fixture-stays-on-phone")
        self.key = self.root / "config/api-key"
        self.key.write_text(KEY + "\n")
        self.key.chmod(0o600)
        self.bundle = self.directory / "bundle"
        self.payload = make_phone_bundle(self.bundle)
        self.remote = remote_namespace()
        self.running = False
        self.actions = []
        self.failure = self.remote["Failure"]

    def tearDown(self):
        self.temp.cleanup()

    def control(self, root, action, launcher=None):
        self.assertEqual(root, self.root)
        self.actions.append(action)
        if action == "status":
            return self.running
        if action == "start":
            self.assertFalse((root / ".deploying").exists())
            self.assertTrue((root / "bin/llama-server").is_file())
            self.running = True
        elif action == "stop":
            self.running = False
        else:
            self.fail("Unexpected control operation: " + action)
        return True

    def existing_code(self):
        old = {}
        for name in self.remote["FILES"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            old[name] = ("old-code-" + name).encode()
            path.write_bytes(old[name])
        return old

    def deploy(self, *, payload=None, probe=None):
        def successful_probe(root, _wait):
            self.assertEqual(root, self.root)
            self.assertTrue(self.running)
            self.actions.append("probe")
        replacements = {"control": self.control, "probe": probe or successful_probe,
                        "require_free_port": lambda: None}
        with mock.patch.dict(self.remote, replacements), contextlib.redirect_stdout(io.StringIO()) as output:
            self.remote["deploy"](self.root, io.BytesIO(payload or self.payload), 0)
        return output.getvalue()

    def assert_private_data_unchanged(self, model_inode):
        self.assertEqual(self.model.read_bytes(), b"GGUFmodel-fixture-stays-on-phone")
        self.assertEqual(self.model.stat().st_ino, model_inode)
        self.assertEqual(self.key.read_text(), KEY + "\n")
        self.assertEqual(list(self.root.glob(".ssh-stage-*")), [])
        self.assertFalse((self.root / ".ssh-deploy-lock").exists())
        self.assertFalse((self.root / ".control-lock").exists())

    def test_success_updates_only_code_preserves_model_key_and_checks_running_server(self):
        self.existing_code()
        self.running = True
        inode = self.model.stat().st_ino
        output = self.deploy()
        self.assertTrue(self.running)
        self.assertIn("probe", self.actions)
        self.assertLess(self.actions.index("start"), self.actions.index("probe"))
        for name in phone.CODE_FILES:
            self.assertEqual((self.root / name).read_bytes(), (self.bundle / name).read_bytes())
        self.assertFalse((self.root / "bin/llama-bench").exists())
        self.assertNotIn(KEY, output)
        self.assert_private_data_unchanged(inode)

    def test_failed_probe_restores_previous_code_and_previous_running_state(self):
        old = self.existing_code()
        self.running = True
        inode = self.model.stat().st_ino
        def failed_probe(_root, _wait):
            raise self.failure("Inference did not succeed.")
        with self.assertRaisesRegex(self.failure, "previous code.*restored"):
            self.deploy(probe=failed_probe)
        self.assertTrue(self.running)
        self.assertEqual(self.actions.count("start"), 2)
        for name, content in old.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertFalse((self.root / ".deploying").exists())
        self.assert_private_data_unchanged(inode)

    def test_failed_first_install_restores_absence_of_code_and_keeps_phone_key(self):
        self.key.unlink()
        def failed_probe(_root, _wait):
            raise self.failure("Inference did not succeed.")
        with self.assertRaisesRegex(self.failure, "previous code.*restored"):
            self.deploy(probe=failed_probe)
        self.assertFalse(self.running)
        self.assertTrue(all(not (self.root / name).exists() for name in self.remote["FILES"]))
        self.assertRegex(self.key.read_bytes(), rb"^[0-9a-f]{64}\n$")
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".ssh-stage-*")), [])

    def test_missing_or_bad_model_fails_before_writes_or_stopping(self):
        self.existing_code()
        for value in (None, b"not-a-GGUF"):
            with self.subTest(model=value):
                self.model.unlink(missing_ok=True)
                if value is not None:
                    self.model.write_bytes(value)
                before = {str(path.relative_to(self.root)): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}
                with self.assertRaises(self.failure):
                    self.deploy()
                after = {str(path.relative_to(self.root)): path.read_bytes()
                         for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
                self.assertEqual(self.actions, [])
                self.assertEqual(list(self.root.glob(".ssh*")), [])

    def test_bad_checksum_or_unsafe_archive_never_stops_existing_server(self):
        self.existing_code()
        self.running = True
        for kind in ("checksum", "traversal", "symlink"):
            with self.subTest(kind=kind):
                target = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(self.payload), mode="r:gz") as original, tarfile.open(fileobj=target, mode="w:gz") as archive:
                    for member in original:
                        data = original.extractfile(member).read()
                        if member.name == "bin/llama-server":
                            if kind == "checksum":
                                data = b"changed"
                            elif kind == "traversal":
                                member.name = "../outside"
                            else:
                                member.type, member.linkname = tarfile.SYMTYPE, "../../outside"
                                data = b""
                            member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                with self.assertRaises(self.failure):
                    self.deploy(payload=target.getvalue())
                self.assertTrue(self.running)
                self.assertEqual(self.actions, [])
                self.assertEqual(list(self.root.glob(".ssh*")), [])
                self.assertFalse((self.directory / "outside").exists())

    def test_probe_keeps_key_on_phone_and_checks_process_after_http_success(self):
        result = subprocess.CompletedProcess([], 0, KEY.encode(), b"")
        self.running = True
        with mock.patch.object(self.remote["subprocess"], "run", return_value=result) as run, mock.patch.dict(self.remote, {"control": self.control}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.remote["probe"](self.root, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(KEY, repr(run.call_args))
        self.assertIn(str(self.key), run.call_args.args[0])
        self.assertEqual(self.actions, ["status"])

    def test_existing_listener_is_detected_without_terminating_it(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(self.failure, "occupied.*manually launched"):
                self.remote["require_free_port"](port)
            self.assertEqual(listener.getsockname()[1], port)
        self.remote["require_free_port"](port)


if __name__ == "__main__":
    unittest.main()
