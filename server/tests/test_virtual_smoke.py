"""Local input/response guards only; these tests do not simulate Android execution."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "virtual/smoke.py"
SPEC = importlib.util.spec_from_file_location("virtual_smoke", SCRIPT)
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class RuntimeGuards(unittest.TestCase):
    def test_serial_requires_one_explicit_loopback_target(self):
        for serial in ("127.0.0.1:6520", "localhost:6520", "0.0.0.0:6520"):
            self.assertEqual(SMOKE.validate_serial(serial), serial)
        for serial in ("", "NX721J", "emulator-5554", "192.168.1.2:5555", "127.0.0.1",
                       "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:6520\nother", "-s phone"):
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                SMOKE.validate_serial(serial)

    def test_http_parser_preserves_multiline_body_and_requires_real_status(self):
        self.assertEqual(SMOKE.parse_http('{\n"content": "text"\n}\n200'),
                         (200, '{\n"content": "text"\n}'))
        self.assertEqual(SMOKE.parse_http("\n401"), (401, ""))
        for value in ("200", "body\n000", "body\n200\n", "body\nerror", "body\n999"):
            with self.subTest(value=value), self.assertRaises(SMOKE.SmokeError):
                SMOKE.parse_http(value)

    def test_completion_requires_generated_tokens_without_quality_assumptions(self):
        self.assertEqual(SMOKE.validate_completion({"content": "nonsense", "tokens_predicted": 8}), 8)
        for value in ({}, [], {"content": 2, "tokens_predicted": 8},
                      {"content": "", "tokens_predicted": 0}, {"content": "x", "tokens_predicted": -1},
                      {"content": "x", "tokens_predicted": True}, {"content": "x", "tokens_predicted": "8"}):
            with self.subTest(value=value), self.assertRaises(SMOKE.SmokeError):
                SMOKE.validate_completion(value)

    def test_guest_guard_rejects_phone_root_or_changed_security_domain(self):
        guest = {"properties": {"ro.product.device": "vsoc_x86_64_only", "ro.product.cpu.abi": "x86_64"},
                 "uid": "2000", "context": "u:r:shell:s0", "selinux": "Enforcing",
                 "framework_processes_observed": ["system_server", "zygote64"]}
        SMOKE.validate_guest(guest)
        for key, value in (("uid", "0"), ("context", "u:r:su:s0"), ("selinux", "Disabled"),
                           ("framework_processes_observed", [])):
            with self.subTest(key=key), self.assertRaises(SMOKE.SmokeError):
                SMOKE.validate_guest({**guest, key: value})
        for key, value in (("ro.product.device", "cerro"), ("ro.product.cpu.abi", "arm64-v8a")):
            with self.subTest(key=key), self.assertRaises(SMOKE.SmokeError):
                SMOKE.validate_guest({**guest, "properties": {**guest["properties"], key: value}})


class BundleGuards(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.bundle = Path(self.temporary.name)
        (self.bundle / "bin").mkdir()
        # A synthetic header exercises validation, never substitutes for a runtime binary.
        header = b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00"
        for name in SMOKE.BINARIES:
            (self.bundle / "bin" / name).write_bytes(header)
        self.provenance = {
            **json.loads((SCRIPT.resolve().parents[1] / "versions.json").read_text()),
            **json.loads(SCRIPT.with_name("versions.json").read_text()),
            "guest_root": SMOKE.GUEST_ROOT, "artifact": "cerro-native-android-x86_64",
            "validation": {"elf_verified": True},
        }
        self.write_provenance()

    def write_provenance(self):
        (self.bundle / "provenance.json").write_text(json.dumps(self.provenance))
        self.write_checksums()

    def write_checksums(self):
        paths = sorted(path for path in self.bundle.rglob("*")
                       if path.is_file() and path.name != "SHA256SUMS")
        (self.bundle / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(self.bundle).as_posix()}\n"
            for path in paths))

    def test_valid_inventory_and_pins(self):
        provenance, pins = SMOKE.verify_bundle(self.bundle)
        self.assertEqual(provenance["abi"], "x86_64")
        self.assertEqual(pins["model_repository"], "ggml-org/models")

    def test_changed_payload_requires_matching_checksum(self):
        with (self.bundle / "bin/sshd").open("ab") as binary:
            binary.write(b"changed")
        with self.assertRaisesRegex(SMOKE.SmokeError, "checksum mismatch"):
            SMOKE.verify_bundle(self.bundle)

    def test_checksum_is_not_a_substitute_for_pinned_provenance(self):
        for key in ("model_revision", "llama_commit", "ndk_version", "android_api"):
            original = self.provenance[key]
            self.provenance[key] = "changed"
            self.write_provenance()
            with self.subTest(key=key), self.assertRaisesRegex(SMOKE.SmokeError, "pinned virtual versions"):
                SMOKE.verify_bundle(self.bundle)
            self.provenance[key] = original

    def test_wrong_abi_rejected_even_with_new_checksum(self):
        binary = self.bundle / "bin/sshd"
        binary.write_bytes(binary.read_bytes()[:18] + b"\xb7\x00")  # ELF e_machine = AArch64.
        self.write_checksums()
        with self.assertRaisesRegex(SMOKE.SmokeError, "x86_64 ELF64"):
            SMOKE.verify_bundle(self.bundle)

    def test_unlisted_files_and_symlinks_rejected(self):
        extra = self.bundle / "unexpected"
        extra.write_text("extra")
        with self.assertRaisesRegex(SMOKE.SmokeError, "file inventory"):
            SMOKE.verify_bundle(self.bundle)
        extra.unlink()
        extra.symlink_to(self.bundle / "bin/sshd")
        with self.assertRaisesRegex(SMOKE.SmokeError, "without symlinks"):
            SMOKE.verify_bundle(self.bundle)

    def test_manifest_path_escape_and_duplicates_rejected(self):
        checksum = self.bundle / "SHA256SUMS"
        good = checksum.read_text()
        for invalid in ("../escape", "/absolute", "bin/../escape", "./bin/sshd"):
            checksum.write_text("0" * 64 + "  " + invalid + "\n")
            with self.subTest(path=invalid), self.assertRaisesRegex(SMOKE.SmokeError, "Unsafe"):
                SMOKE.verify_bundle(self.bundle)
        checksum.write_text(good + good.splitlines()[0] + "\n")
        with self.assertRaisesRegex(SMOKE.SmokeError, "duplicate"):
            SMOKE.verify_bundle(self.bundle)


if __name__ == "__main__":
    unittest.main()
