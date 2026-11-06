# NX721J server firmware

This fork targets the Nubia Z60 Ultra (NX721J, PQ83A01, cerro, Snapdragon SM8650). The starting point is the upstream `lineage-23.2` device tree at `a7ed9a2ab78485804ed384c3fae089a3b291494b`.

Build and verify an unmodified cerro baseline first. This repository belongs at `device/nubia/cerro` in a matching LineageOS source checkout. Follow `lineage.dependencies` and the common device tree for the kernel, modules and device trees. Resolve the proprietary vendor projects against the same branch or extract them from a compatible image using the upstream scripts. Record the source revisions used for a build.

Add a separate product for the server variant so the standard cerro target remains a comparison point. Inventory inherited packages before removing any. Keep the vendor integration for UFS, USB, Wi-Fi, audio, charging, thermal management and Adreno compute. Confirm these on NX721J before changing the kernel, proprietary blobs, init services or SELinux policy.

Measure idle memory and memory under the same inference workload as the baseline. Verify SSH and an authenticated inference request with the screen off, then test recovery after a reboot. Record build and device-test results separately.

Keep personal files, backups, credentials, signing keys and device identifiers out of Git. Treat bootloader changes, factory resets, flashing and relocking as separate operations that require explicit authorization for this device.
