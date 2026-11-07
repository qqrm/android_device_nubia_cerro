#!/system/bin/sh
# Read-only Android baseline. Redirect stdout to a private file outside the repo.
# No root request, command-line arguments, personal files, or unique device IDs.
set -u
PATH=/system/bin:/system/xbin:${PATH:-}
LC_ALL=C
export PATH LC_ALL
failures=0
output=

section() { printf '\n=== %s ===\n' "$1"; }

capture() {
    operation=$1
    shift
    if output=$("$@" 2>/dev/null); then
        return 0
    else
        status=$?
        failures=$((failures + 1))
        printf 'ERROR: %s (exit=%s)\n' "$operation" "$status"
        output=
        return 1
    fi
}

show() {
    if capture "$@"; then
        printf '%s\n' "$output"
    fi
}

filter_services() {
    # Only init service names and the documented service states leave this filter.
    printf '%s\n' "$output" | awk '
        /^\[init\.svc\.[A-Za-z0-9_.-]+\]: \[(running|stopped|restarting)\]$/ {
            sub(/^\[/, ""); sub(/\]: \[/, "="); sub(/\]$/, ""); print
        }'
}

filter_packages() {
    printf '%s\n' "$output" | awk '
        /^package:[A-Za-z0-9_.]+$/ {sub(/^package:/, ""); print}'
}

section metadata
printf '%s\n' 'format=cerro-inventory-v1'
if capture timestamp date -u '+%Y-%m-%dT%H:%M:%SZ'; then
    printf 'utc=%s\n' "$output"
fi
if capture uptime awk '{print $1; exit}' /proc/uptime; then
    printf 'uptime_seconds=%s\n' "$output"
fi
printf '%s\n' 'visibility=current shell; permissions may hide processes or services'

section shell_and_kernel
show identity id
if capture kernel uname -r; then
    printf 'kernel_release=%s\n' "$output"
fi
if su_path=$(command -v su 2>/dev/null); then
    printf 'su_command=%s (presence only; not executed)\n' "$su_path"
else
    printf '%s\n' 'su_command=absent from PATH'
fi

section properties
for property in \
    ro.product.model ro.product.device ro.product.cpu.abilist \
    ro.build.version.release ro.build.version.sdk ro.build.type ro.debuggable \
    ro.boot.flash.locked ro.boot.vbmeta.device_state ro.boot.verifiedbootstate \
    ro.boot.slot_suffix ro.bootmode ro.boot.mode ro.zygote
do
    if capture "getprop $property" /system/bin/getprop "$property"; then
        printf '%s=%s\n' "$property" "${output:-[unset]}"
    fi
done

section memory_kib
show memory awk '
    /^(MemTotal|MemFree|MemAvailable|Buffers|Cached|Slab|SReclaimable|SwapTotal|SwapFree):/ {
        print
    }' /proc/meminfo

section visible_processes
# Android toybox CMD without -f is the kernel comm, not argv. Do not fall back
# to another ps implementation or its default columns, which can expose argv.
printf '%s\n' 'RSS is KiB; CMD is the kernel process name, possibly truncated.'
show processes /system/bin/ps -A -o UID,PID,RSS,CMD

section system_packages
if capture system_packages /system/bin/pm list packages -s; then
    show package_names filter_packages
fi

section init_services
# getprop has no prefix query; keep the full listing in memory only, then filter.
# Never print the unfiltered listing or getprop diagnostics.
if capture init_services /system/bin/getprop; then
    show service_states filter_services
fi
output=

section result
printf 'failed_commands=%s\n' "$failures"
printf '%s\n' 'No device changes requested. This report is not a complete hardware validation.'
[ "$failures" -eq 0 ]
