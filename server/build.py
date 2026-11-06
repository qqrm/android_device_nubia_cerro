#!/usr/bin/env python3
"""Build and package only the native Android tools, without an AOSP checkout."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("llama-server", "llama-bench")
ARTIFACT = "cerro-server-android-arm64-cpu"
SYSTEM_LIBS = {"libc.so", "libm.so", "libdl.so", "liblog.so", "libandroid.so"}


def run(*args, cwd=None, capture=False):
    return subprocess.run(
        [str(arg) for arg in args], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def load_versions():
    data = json.loads((ROOT / "server/versions.json").read_text())
    if data["llama_repository"] != "ggml-org/llama.cpp":
        raise ValueError("Unexpected upstream repository")
    if not re.fullmatch(r"[0-9a-f]{40}", data["llama_commit"]):
        raise ValueError("llama_commit must be a complete commit SHA")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", data["ndk_version"]):
        raise ValueError("Invalid NDK version")
    if data["abi"] != "arm64-v8a" or data["android_api"] != 28:
        raise ValueError("This kit targets Android API 28 / arm64-v8a")
    return data


def verify_elf(readelf, binary):
    report = run(readelf, "--file-header", "--program-headers", "--dynamic",
                 "--notes", binary, capture=True)
    required = (r"Class:\s+ELF64", r"Data:.*little endian",
                r"Type:\s+DYN", r"Machine:\s+AArch64",
                r"Requesting program interpreter: /system/bin/linker64")
    if not all(re.search(pattern, report) for pattern in required):
        raise ValueError(f"{binary.name}: not an Android ARM64 PIE executable")
    needed = re.findall(r"\(NEEDED\).*Shared library: \[([^]]+)\]", report)
    unexpected = set(needed) - SYSTEM_LIBS
    if unexpected or "libc.so" not in needed:
        raise ValueError(f"{binary.name}: unexpected runtime libraries: {needed}")
    if re.search(r"\((?:RPATH|RUNPATH)\)", report):
        raise ValueError(f"{binary.name}: unexpected runtime search path")
    return report, needed


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_licenses(source, ndk, destination):
    destination.mkdir(parents=True)
    shutil.copy2(source / "LICENSE", destination / "llama.cpp-LICENSE")
    shutil.copytree(source / "licenses", destination / "upstream")
    # Vendored headers contain additional notices; include their original text.
    shutil.copytree(source / "vendor", destination / "vendor")
    notices = [ndk / "NOTICE", ndk / "NOTICE.toolchain",
               ndk / "toolchains/llvm/prebuilt/linux-x86_64/NOTICE",
               ndk / "toolchains/llvm/prebuilt/linux-x86_64/NOTICE.toolchain"]
    copied = []
    for notice in notices:
        if notice.is_file():
            target = destination / "ndk" / notice.relative_to(ndk)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(notice, target)
            copied.append(str(notice.relative_to(ndk)))
    if not copied:
        raise ValueError("NDK license notices were not found")
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-outputs", action="store_true")
    parser.add_argument("--source", type=Path, default=ROOT / ".build/llama.cpp")
    parser.add_argument("--ndk", type=Path)
    parser.add_argument("--build-dir", type=Path, default=ROOT / ".build/android-cpu")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    versions = load_versions()
    if args.github_outputs:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key in ("llama_commit", "ndk_version"):
                output.write(f"{key}={versions[key]}\n")
        return
    if args.ndk is None or not 1 <= args.jobs <= 16:
        parser.error("--ndk is required; --jobs must be between 1 and 16")
    source, ndk = args.source.resolve(), args.ndk.resolve()
    build, dist = args.build_dir.resolve(), args.dist.resolve()
    package = dist / "cerro-server"
    if package.exists():
        raise ValueError("Output payload already exists; choose a fresh --dist directory")
    actual_sha = run("git", "rev-parse", "HEAD", cwd=source, capture=True).strip()
    if actual_sha != versions["llama_commit"]:
        raise ValueError(f"Upstream SHA mismatch: {actual_sha}")
    if run("git", "status", "--porcelain", cwd=source, capture=True).strip():
        raise ValueError("Upstream checkout must be clean")
    properties = (ndk / "source.properties").read_text()
    revision = re.search(r"^Pkg.Revision\s*=\s*(\S+)", properties, re.MULTILINE)
    if revision is None or revision.group(1) != versions["ndk_version"]:
        raise ValueError("NDK revision does not match server/versions.json")
    llvm = ndk / "toolchains/llvm/prebuilt/linux-x86_64/bin"
    flags = [
        "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_SKIP_RPATH=ON",
        f"-DCMAKE_TOOLCHAIN_FILE={ndk}/build/cmake/android.toolchain.cmake",
        "-DANDROID_ABI=arm64-v8a", "-DANDROID_PLATFORM=android-28",
        "-DANDROID_STL=c++_static", "-DBUILD_SHARED_LIBS=OFF",
        "-DGGML_STATIC=OFF", "-DGGML_BACKEND_DL=OFF",
        "-DGGML_NATIVE=OFF", "-DGGML_CPU_ALL_VARIANTS=OFF", "-DGGML_CCACHE=OFF",
        "-DGGML_CPU_KLEIDIAI=OFF", "-DGGML_OPENMP=OFF",
        "-DGGML_LLAMAFILE=OFF", "-DGGML_OPENCL=OFF",
        "-DGGML_VULKAN=OFF", "-DGGML_BLAS=OFF", "-DGGML_RPC=OFF",
        "-DLLAMA_BUILD_COMMON=ON", "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_SERVER=ON", "-DLLAMA_BUILD_APP=OFF",
        "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_UI=OFF", "-DLLAMA_USE_PREBUILT_UI=OFF",
        "-DLLAMA_OPENSSL=OFF", "-DLLAMA_BUILD_BORINGSSL=OFF",
        "-DLLAMA_BUILD_LIBRESSL=OFF", "-DLLAMA_SUBPROCESS=OFF",
        "-DLLAMA_LLGUIDANCE=OFF", "-DLLAMA_CONNECT=OFF",
    ]
    started = time.monotonic()
    run("cmake", "-S", source, "-B", build, "-G", "Ninja", *flags)
    run("cmake", "--build", build, "--parallel", args.jobs,
        "--target", *TOOLS)
    elapsed = round(time.monotonic() - started, 1)
    # Refuse to reuse a payload directory to prevent packaging stale files.
    package.mkdir(parents=True, exist_ok=False)
    (package / "bin").mkdir()
    reports, dependencies = [], {}
    for name in TOOLS:
        target = package / "bin" / name
        shutil.copy2(build / "bin" / name, target)
        run(llvm / "llvm-strip", "--strip-unneeded", target)
        report, needed = verify_elf(llvm / "llvm-readelf", target)
        reports.append(f"=== {name} ===\n{report}")
        dependencies[name] = needed
        target.chmod(0o755)
    shutil.copy2(ROOT / "server/cerro.py", package / "cerro.py")
    shutil.copy2(ROOT / "server/phone.py", package / "phone.py")
    shutil.copytree(ROOT / "server/device", package / "device")
    shutil.copy2(ROOT / "server/README.md", package / "README.md")
    shutil.copytree(ROOT / "server/termux", package / "termux")
    notices = copy_licenses(source, ndk, package / "licenses")
    provenance = {
        "schema_version": 1, **versions,
        "kit_commit": run("git", "rev-parse", "HEAD", cwd=ROOT, capture=True).strip(),
        "backend": "cpu", "cpu_baseline": "armv8-a (NDK default)",
        "cmake_flags": flags, "targets": list(TOOLS),
        "android_runtime_libraries": dependencies, "ndk_notices": notices,
        "validation": {"cross_compiled": True, "elf_verified": True,
                       "executed_on_android": False,
                       "screen_off_tested": False, "reboot_tested": False},
    }
    (package / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    files = sorted(path for path in package.rglob("*") if path.is_file())
    if any(path.is_symlink() for path in package.rglob("*")):
        raise ValueError("Payload must not contain symlinks")
    sums = "".join(f"{sha256(path)}  {path.relative_to(package).as_posix()}\n"
                   for path in files)
    (package / "SHA256SUMS").write_text(sums, encoding="utf-8")
    archive = dist / f"{ARTIFACT}.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        bundle.add(package, arcname="cerro-server")
    (dist / f"{archive.name}.sha256").write_text(
        f"{sha256(archive)}  {archive.name}\n", encoding="utf-8")
    report = {
        "build_seconds": elapsed, "build_jobs": args.jobs,
        "archive_bytes": archive.stat().st_size,
        "binary_bytes": {name: (package / "bin" / name).stat().st_size for name in TOOLS},
        "payload_bytes": sum(path.stat().st_size for path in files),
        "build_directory_bytes": sum(p.stat().st_size for p in build.rglob("*") if p.is_file()),
        "source_checkout_bytes": sum(p.stat().st_size for p in source.rglob("*") if p.is_file()),
        "ndk_install_bytes": sum(p.stat().st_size for p in ndk.rglob("*") if p.is_file()),
        "android_execution": "not tested; requires a device",
    }
    (dist / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    (dist / "elf-report.txt").write_text("\n".join(reports))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Android ARM64 CPU kit\n\n")
            summary.write(f"Pinned llama.cpp: `{actual_sha}`\n\n")
            summary.write(f"Build: **{elapsed:.0f} s**; archive: **{archive.stat().st_size / 1048576:.1f} MiB**.\n\n")
            summary.write("Cross-compilation and ELF checks passed. Device execution, inference, screen-off and reboot tests are pending.\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
