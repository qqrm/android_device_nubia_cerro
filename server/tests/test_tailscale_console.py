"""Trust boundary checks for the console helper, not Android/network emulation."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "termux" / "tailscale-console.sh"


class TailscaleConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cerro-ts-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.prefix = self.root / "usr"
        self.commands = self.prefix / "bin"
        self.commands.mkdir(parents=True)
        (self.commands / "sh").symlink_to("/bin/sh")
        self.stub(self.commands / "uname", "printf 'aarch64\\n'\n")
        self.env = dict(os.environ, HOME=str(self.home), PREFIX=str(self.prefix))
        self.env["PATH"] = str(self.commands) + os.pathsep + os.environ["PATH"]
        self.storage = self.home / ".local" / "share" / "cerro-tailscale"
        self.release = self.storage / "bin" / "1.104.4"

    def stub(self, path, body):
        path.write_text("#!/bin/sh\nset -eu\n" + body)
        path.chmod(0o700)

    def run_script(self, *args):
        return subprocess.run(
            ["sh", str(SCRIPT), *args], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def fake_install(self, client_body):
        self.release.mkdir(parents=True)
        self.stub(self.release / "tailscale", client_body)
        self.stub(self.release / "tailscaled", "exit 99\n")

    def test_bad_download_never_reaches_archive_extraction(self):
        self.stub(self.commands / "curl", """
while [ "$#" -gt 0 ]; do
    if [ "$1" = --output ]; then shift; printf 'corrupt archive' > "$1"; exit 0; fi
    shift
done
exit 99
""")
        self.stub(self.commands / "tar", 'touch "$HOME/tar-was-called"\nexit 99\n')
        result = self.run_script("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.home / "tar-was-called").exists())
        self.assertFalse(self.release.exists())
        self.assertFalse((self.storage / ".install-lock").exists())
        self.assertEqual(list(self.storage.glob(".install.*")), [])

    def test_symlinked_storage_is_rejected_without_writing_outside_home(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "untouched"
        marker.write_text("preserve")
        self.storage.parent.mkdir(parents=True)
        self.storage.symlink_to(outside, target_is_directory=True)
        result = self.run_script("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [marker])
        self.assertEqual(marker.read_text(), "preserve")

    def test_login_hides_native_output_and_passes_only_key_file_path(self):
        secret = "tskey-auth-test-never-print-this-value"
        self.fake_install("""
printf '%s\\n' "$@" > "$HOME/client-args"
printf 'tskey-auth-test-never-print-this-value\\n'
printf 'tskey-auth-test-never-print-this-value\\n' >&2
exit 1
""")
        key = self.home / "auth-key"
        key.write_text(secret + "\n")
        key.chmod(0o600)
        result = self.run_script("login", str(key))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, result.stdout + result.stderr)
        args = (self.home / "client-args").read_text()
        self.assertNotIn(secret, args)
        self.assertIn("--auth-key=file:" + str(key), args)
        self.assertEqual(self.storage.stat().st_mode & 0o777, 0o700)
        log = self.storage / "login.log"
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn(secret, log.read_text())

    def test_readable_by_others_or_symlinked_key_is_rejected_before_login(self):
        self.fake_install('touch "$HOME/client-was-called"\n')
        key = self.home / "auth-key"
        key.write_text("test-private-value\n")
        key.chmod(0o644)
        for path in (key, self.home / "key-link"):
            if path != key:
                key.chmod(0o600)
                path.symlink_to(key)
            with self.subTest(path=path.name):
                result = self.run_script("login", str(path))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / "client-was-called").exists())
                self.assertNotIn("test-private-value", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
