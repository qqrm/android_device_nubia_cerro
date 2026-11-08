#!/usr/bin/env python3
"""Cross-build native x86_64 Android binaries for an isolated Cuttlefish test.

This payload is separate from the ARM64 phone kit. It does not build an Android
image, install services, generate credentials, or deploy anything.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cerro_native_build", ROOT / "server/build.py")
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)
GUEST_ROOT = "/data/local/tmp/cerro-native"
PAYLOAD = "cerro-native-x86_64"
ARTIFACT = "cerro-native-android-x86_64"
SSH_TOOLS = ("sshd", "sshd-session", "sshd-auth")


def run(*args, cwd=None, env=None, capture=False):
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def load_versions():
    # Reuse and validate the phone kit's pinned llama.cpp, NDK and API. Its ABI
    # guard is unchanged; only this separate builder selects x86_64.
    versions = COMMON.load_versions()
    virtual = json.loads((Path(__file__).with_name("versions.json")).read_text())
    if virtual["schema_version"] != 1 or virtual["abi"] != "x86_64":
        raise ValueError("Unsupported virtual native payload version or ABI")
    for name, repository in (("openssh", "openssh/openssh-portable"),
                             ("openssl", "openssl/openssl")):
        if virtual[f"{name}_repository"] != repository:
            raise ValueError(f"Unexpected {name} repository")
        if not re.fullmatch(r"[0-9a-f]{40}", virtual[f"{name}_commit"]):
            raise ValueError(f"{name}_commit must be a complete commit SHA")
    if (virtual["model_repository"] != "ggml-org/models"
            or virtual["model_file"] != "tinyllamas/stories260K.gguf"
            or not re.fullmatch(r"[0-9a-f]{40}", virtual["model_revision"])
            or not re.fullmatch(r"[0-9a-f]{64}", virtual["model_sha256"])
            or not isinstance(virtual["model_bytes"], int)
            or virtual["model_bytes"] <= 0):
        raise ValueError("Invalid pinned inference test model")
    return {**versions, **virtual}


def verify_source(source, expected):
    actual = run("git", "rev-parse", "HEAD", cwd=source, capture=True).strip()
    if actual != expected:
        raise ValueError(f"{source.name}: source SHA mismatch: {actual}")
    if run("git", "status", "--porcelain", cwd=source, capture=True).strip():
        raise ValueError(f"{source.name}: upstream checkout must be clean")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-outputs", action="store_true")
    parser.add_argument("--llama-source", type=Path, default=ROOT / ".build/src/llama.cpp")
    parser.add_argument("--openssh-source", type=Path, default=ROOT / ".build/src/openssh")
    parser.add_argument("--openssl-source", type=Path, default=ROOT / ".build/src/openssl")
    parser.add_argument("--ndk", type=Path)
    parser.add_argument("--build-dir", type=Path, default=ROOT / ".build/virtual-native")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist/virtual-native")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    versions = load_versions()
    if args.github_outputs:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key in ("llama_commit", "ndk_version", "openssh_commit", "openssl_commit"):
                output.write(f"{key}={versions[key]}\n")
        return
    if args.ndk is None or not 1 <= args.jobs <= 16:
        parser.error("--ndk is required; --jobs must be between 1 and 16")
    sources = {"llama": args.llama_source.resolve(),
               "openssh": args.openssh_source.resolve(),
               "openssl": args.openssl_source.resolve()}
    for name, source in sources.items():
        verify_source(source, versions[f"{name}_commit"])
    ndk, build, dist = args.ndk.resolve(), args.build_dir.resolve(), args.dist.resolve()
    revision = re.search(r"^Pkg.Revision\s*=\s*(\S+)",
                         (ndk / "source.properties").read_text(), re.MULTILINE)
    if revision is None or revision.group(1) != versions["ndk_version"]:
        raise ValueError("NDK revision does not match server/versions.json")
    package = dist / PAYLOAD
    if build.exists() or package.exists():
        raise ValueError("Choose fresh --build-dir and --dist payload directories")
    llvm = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    compiler = llvm / f"x86_64-linux-android{versions['android_api']}-clang"
    if not compiler.is_file():
        raise ValueError("The pinned Linux x86_64 NDK toolchain is required")
    build.mkdir(parents=True)
    started = time.monotonic()

    llama_build = build / "llama"
    llama_flags = COMMON.llama_cmake_flags(ndk, abi=versions["abi"])
    run("cmake", "-S", sources["llama"], "-B", llama_build, "-G", "Ninja", *llama_flags)
    run("cmake", "--build", llama_build, "--parallel", args.jobs, "--target", "llama-server")

    # Follow upstream NOTES-ANDROID.md: explicit Android target/API and the NDK
    # toolchain on PATH. libcrypto is static PIC, while libc remains Android's.
    openssl_build, openssl_install = build / "openssl", build / "openssl-install"
    openssl_build.mkdir()
    env = os.environ.copy()
    env.update(ANDROID_NDK_ROOT=str(ndk), PATH=f"{llvm}{os.pathsep}{env['PATH']}")
    openssl_flags = [
        "android-x86_64", f"-D__ANDROID_API__={versions['android_api']}",
        "no-shared", "no-tests", "-fPIC", f"--prefix={openssl_install}",
        f"--openssldir={GUEST_ROOT}/ssl", "--libdir=lib",
    ]
    run("perl", sources["openssl"] / "Configure", *openssl_flags,
        cwd=openssl_build, env=env)
    run("make", f"-j{args.jobs}", "build_libs", cwd=openssl_build, env=env)
    run("make", "install_dev", cwd=openssl_build, env=env)
    if not (openssl_install / "lib/libcrypto.a").is_file():
        raise ValueError("The static Android libcrypto archive was not installed")

    # Autoreconf generates files in the source directory. Work on a copy so the
    # verified upstream checkout remains unchanged and attributable to its SHA.
    ssh_source, ssh_build = build / "openssh-source", build / "openssh"
    shutil.copytree(sources["openssh"], ssh_source, ignore=shutil.ignore_patterns(".git"))
    run("autoreconf", "-fi", cwd=ssh_source)
    ssh_build.mkdir()
    ssh_env = env.copy()
    # NDK Clang supports sentinel attributes. OpenSSH's empty fallback macro
    # otherwise breaks Bionic's __sentinel__(1) declaration in <unistd.h>.
    # This is the same feature selection as AOSP OpenSSH commit 819a147a5358.
    ssh_env.update(CC=str(compiler), AR=str(llvm / "llvm-ar"),
                   RANLIB=str(llvm / "llvm-ranlib"), STRIP=str(llvm / "llvm-strip"),
                   CFLAGS="-O2 -fPIE", CPPFLAGS="-DHAVE_ATTRIBUTE__SENTINEL__=1",
                   LDFLAGS="-pie")
    build_host = run("sh", ssh_source / "config.guess", capture=True).strip()
    ssh_flags = [
        f"--build={build_host}", "--host=x86_64-linux-android",
        f"--prefix={GUEST_ROOT}", f"--bindir={GUEST_ROOT}/bin",
        f"--sbindir={GUEST_ROOT}/bin", f"--libexecdir={GUEST_ROOT}/bin",
        f"--sysconfdir={GUEST_ROOT}/etc", f"--with-pid-dir={GUEST_ROOT}",
        "--with-privsep-user=shell", f"--with-privsep-path={GUEST_ROOT}/empty",
        "--with-default-path=/system/bin", f"--with-ssl-dir={openssl_install}",
        "--without-rpath", "--without-zlib", "--without-pam",
    ]
    # Keep upstream sandbox autodetection and privilege-separation behavior.
    # The runtime harness must start sshd as shell, never as root with this user.
    run(ssh_source / "configure", *ssh_flags, cwd=ssh_build, env=ssh_env)
    config = (ssh_build / "config.h").read_text()
    if "#define WITH_OPENSSL 1" not in config or "#define USE_PAM 1" in config:
        raise ValueError("Unexpected OpenSSH crypto or PAM configuration")
    if not all(f"#define {name} 1" in config for name in ("DISABLE_UTMP", "DISABLE_WTMP")):
        raise ValueError("OpenSSH did not select its upstream Android configuration")
    run("make", f"-j{args.jobs}", *SSH_TOOLS, cwd=ssh_build, env=ssh_env)

    (package / "bin").mkdir(parents=True, exist_ok=False)
    binaries = {"llama-server": llama_build / "bin/llama-server",
                **{name: ssh_build / name for name in SSH_TOOLS}}
    reports, dependencies = [], {}
    for name, binary in binaries.items():
        target = package / "bin" / name
        shutil.copy2(binary, target)
        run(llvm / "llvm-strip", "--strip-unneeded", target)
        report, needed = COMMON.verify_elf(llvm / "llvm-readelf", target, abi=versions["abi"])
        reports.append(f"=== {name} ===\n{report}")
        dependencies[name] = needed
        target.chmod(0o755)
    notices = COMMON.copy_licenses(sources["llama"], ndk, package / "licenses/llama-and-ndk")
    for name, license_file in (("openssh", "LICENCE"), ("openssl", "LICENSE.txt")):
        destination = package / "licenses" / name
        destination.mkdir()
        shutil.copy2(sources[name] / license_file, destination / license_file)
        for notice in sources[name].glob("NOTICE*"):
            if notice.is_file():
                shutil.copy2(notice, destination / notice.name)
    elapsed = round(time.monotonic() - started, 1)
    provenance = {
        **versions, "artifact": ARTIFACT, "purpose": "isolated Cuttlefish native test",
        "kit_commit": run("git", "rev-parse", "HEAD", cwd=ROOT, capture=True).strip(),
        "backend": "cpu", "cpu_baseline": "Android NDK x86_64 ABI; optional ggml ISA disabled",
        "guest_root": GUEST_ROOT, "targets": list(binaries), "cmake_flags": llama_flags,
        "openssl_configure_flags": openssl_flags, "openssh_configure_flags": ssh_flags,
        "openssh_build_environment": {key: ssh_env[key] for key in ("CC", "AR", "RANLIB", "CFLAGS", "CPPFLAGS", "LDFLAGS")},
        "openssh_sandbox": "upstream autodetection; no override",
        "android_runtime_libraries": dependencies, "ndk_notices": notices,
        "validation": {"cross_compiled": True, "elf_verified": True,
                       "executed_on_android": False, "phone_tested": False},
    }
    (package / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    files = sorted(path for path in package.rglob("*") if path.is_file())
    if any(path.is_symlink() for path in package.rglob("*")):
        raise ValueError("Payload must not contain symlinks")
    (package / "SHA256SUMS").write_text("".join(
        f"{COMMON.sha256(path)}  {path.relative_to(package).as_posix()}\n" for path in files))
    archive = dist / f"{ARTIFACT}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        bundle.add(package, arcname=PAYLOAD)
    (dist / f"{archive.name}.sha256").write_text(f"{COMMON.sha256(archive)}  {archive.name}\n")
    report = {"build_seconds": elapsed, "build_jobs": args.jobs,
              "archive_bytes": archive.stat().st_size,
              "binary_bytes": {name: (package / "bin" / name).stat().st_size for name in binaries},
              "android_execution": "not tested by this build; requires the virtual runtime test"}
    (dist / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    (dist / "elf-report.txt").write_text("\n".join(reports))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
