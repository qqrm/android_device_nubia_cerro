"""Bootstrap trust/ownership checks; the SSH daemon and phone are not simulated as hardware tests."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BOOTSTRAP = Path(__file__).resolve().parents[1] / "termux" / "bootstrap-ssh.sh"


@unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH keygen is required")
class BootstrapSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cerro-ssh-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.phone_home = self.root / "phone"
        self.phone_home.mkdir()
        self.prefix = self.root / "usr"
        binaries = self.prefix / "bin"
        binaries.mkdir(parents=True)
        for name, target in (
            ("sh", shutil.which("sh")),
            ("ssh-keygen", shutil.which("ssh-keygen")),
            ("python", sys.executable),
        ):
            (binaries / name).symlink_to(target)
        self.stub("sshd", """
case "$1" in
    -t)
        printf 'validate\\n' >> "$PREFIX/sshd-events"
        [ "${CERRO_TEST_INVALID_CONFIG:-0}" = 0 ] || exit 1
        ;;
    -f)
        printf 'start\\n' >> "$PREFIX/sshd-events"
        [ "${CERRO_TEST_BUSY_PORT:-0}" = 0 ] || exit 1
        touch "$PREFIX/daemon-started"
        ;;
    *) exit 99 ;;
esac
""")
        self.stub("ssh-keyscan", """
[ -f "$PREFIX/daemon-started" ] || exit 1
key=$("$PREFIX/bin/ssh-keygen" -y -P '' -f "$HOME/.ssh/cerro-sshd/ssh_host_ed25519_key")
printf '[127.0.0.1]:8022 %s\\n' "$key"
""")
        self.stub("termux-wake-lock", 'touch "$PREFIX/wake-lock-called"\n')
        self.env = dict(os.environ, HOME=str(self.phone_home), PREFIX=str(self.prefix))
        self.key = self.root / "deploy-key"
        subprocess.run(
            [shutil.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(self.key)],
            check=True, capture_output=True,
        )
        self.public = self.key.with_suffix(".pub")
        self.state = self.phone_home / ".ssh" / "cerro-sshd"
        self.original_authorized = self.phone_home / ".ssh" / "authorized_keys"
        self.original_authorized.parent.mkdir()
        self.original_authorized.write_text("existing normal SSH keys\n")
        self.original_config = self.prefix / "etc" / "ssh" / "sshd_config"
        self.original_config.parent.mkdir(parents=True)
        self.original_config.write_text("existing normal SSH configuration\n")

    def stub(self, name, body):
        path = self.prefix / "bin" / name
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o700)

    def run_bootstrap(self, *options, public=None, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["sh", str(BOOTSTRAP), str(public or self.public), *options],
            env=env, capture_output=True, text=True, timeout=15,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_key_rejected_before_ssh_changes(self):
        result = self.run_bootstrap(public=self.key)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)

    def test_key_only_config_preserves_existing_ssh_and_pins_own_host_key(self):
        # A public key copied from Windows is accepted, without copying its CRLF.
        self.public.write_bytes(self.public.read_bytes().replace(b"\n", b"\r\n"))
        result = self.run_bootstrap()
        self.assert_success(result)
        self.assertEqual(self.original_authorized.read_text(), "existing normal SSH keys\n")
        self.assertEqual(self.original_config.read_text(), "existing normal SSH configuration\n")
        config = (self.state / "sshd_config").read_text()
        for setting in (
            "AuthenticationMethods publickey", "PasswordAuthentication no",
            "KbdInteractiveAuthentication no", "AllowTcpForwarding local",
            "PermitOpen 127.0.0.1:8080", "AllowAgentForwarding no",
            "X11Forwarding no", "PermitTTY no", "PermitUserRC no",
            "Subsystem sftp internal-sftp",
        ):
            self.assertIn(setting + "\n", config)
        authorized = (self.state / "authorized_keys").read_text()
        self.assertIn('restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 ', authorized)
        host_public = (self.state / "ssh_host_ed25519_key.pub").read_text().split()
        self.assertIn(f"cerro-phone {host_public[0]} {host_public[1]}\n", result.stdout)
        self.assertNotIn("[cerro-phone]:8022", result.stdout)
        self.assertNotIn("PRIVATE KEY", result.stdout + result.stderr)
        for name in ("authorized_keys", "sshd_config", "ssh_host_ed25519_key"):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)
        for name in ("cerro-server", "cerro-server/models", "cerro-server/config"):
            directory = self.phone_home / name
            self.assertTrue(directory.is_dir())
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.prefix / "wake-lock-called").exists())

    def test_rerun_preserves_host_key_and_does_not_duplicate_key_or_daemon(self):
        self.assert_success(self.run_bootstrap("--wake-lock"))
        host_private = (self.state / "ssh_host_ed25519_key").read_bytes()
        authorized = (self.state / "authorized_keys").read_bytes()
        model = self.phone_home / "cerro-server" / "models" / "model.gguf"
        model.write_bytes(b"GGUFexisting-model")
        api_key = self.phone_home / "cerro-server" / "config" / "api-key"
        api_key.write_text("existing-api-key\n")
        self.assert_success(self.run_bootstrap())
        self.assertEqual((self.state / "ssh_host_ed25519_key").read_bytes(), host_private)
        self.assertEqual((self.state / "authorized_keys").read_bytes(), authorized)
        self.assertEqual(model.read_bytes(), b"GGUFexisting-model")
        self.assertEqual(api_key.read_text(), "existing-api-key\n")
        self.assertEqual((self.prefix / "sshd-events").read_text().splitlines().count("start"), 1)
        self.assertTrue((self.prefix / "wake-lock-called").is_file())

    def test_existing_custom_cerro_config_is_not_overwritten(self):
        self.assert_success(self.run_bootstrap())
        config = self.state / "sshd_config"
        custom = config.read_text() + "# user change\n"
        config.write_text(custom)
        authorized = (self.state / "authorized_keys").read_bytes()
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs", result.stderr)
        self.assertEqual(config.read_text(), custom)
        self.assertEqual((self.state / "authorized_keys").read_bytes(), authorized)

    def test_invalid_daemon_config_does_not_start_or_install_boot_helper(self):
        result = self.run_bootstrap(extra_env={"CERRO_TEST_INVALID_CONFIG": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.prefix / "daemon-started").exists())
        self.assertFalse((self.phone_home / ".termux" / "boot" / "20-cerro-sshd").exists())
        self.assertFalse((self.state / "authorized_keys").exists())

    def test_busy_port_fails_without_claiming_success(self):
        result = self.run_bootstrap(extra_env={"CERRO_TEST_BUSY_PORT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("port 8022", result.stderr)
        self.assertNotIn("SSH is ready", result.stdout)

    def test_model_directory_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        server = self.phone_home / "cerro-server"
        server.mkdir()
        (server / "models").symlink_to(outside, target_is_directory=True)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ordinary directories", result.stderr)
        self.assertFalse((self.prefix / "daemon-started").exists())
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(shutil.which("sshd"), "OpenSSH server is required to parse the generated configuration")
    def test_real_openssh_accepts_configuration(self):
        self.assert_success(self.run_bootstrap())
        result = subprocess.run(
            [shutil.which("sshd"), "-T", "-f", str(self.state / "sshd_config")],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode and result.stderr.strip().startswith("Missing privilege separation directory:"):
            self.skipTest("This executor has no OpenSSH privilege-separation directory; CI runs this check as its ordinary runner user")
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = result.stdout.splitlines()
        for expected in (
            "port 8022", "authenticationmethods publickey", "passwordauthentication no",
            "kbdinteractiveauthentication no", "allowtcpforwarding local",
            "permitopen 127.0.0.1:8080", "permittty no", "subsystem sftp internal-sftp",
        ):
            self.assertIn(expected, settings)


if __name__ == "__main__":
    unittest.main()
