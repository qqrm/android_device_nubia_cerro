#!/data/data/com.termux/files/usr/bin/sh
# Keep this script at a stable path in Termux HOME; run it with Termux sh.
# Foreground `run` can be supervised by the caller. It never changes Android's VPN.
# Upstream v1.104.4 includes androidbin/androiddns for raw executables on Android:
# https://github.com/tailscale/tailscale/tree/v1.104.4/feature/androidbin
set -eu
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }
usage() { fail 'Usage: sh tailscale-console.sh install | run | login AUTH_KEY_FILE | status'; }
cleanup_install() {
    if [ -n "$work" ]; then rm -rf "$work"; fi
    rmdir "$storage/.install-lock" 2>/dev/null || true
}
[ "$#" -ge 1 ] || usage
action=$1
case $action in
    install|run|status) [ "$#" -eq 1 ] || usage ;;
    login) [ "$#" -eq 2 ] || usage ;;
    *) usage ;;
esac

if [ -z "${HOME:-}" ] || [ -z "${PREFIX:-}" ]; then
    fail 'Run this script inside Termux.'
fi
case $HOME in /*) ;; *) fail 'Termux HOME must be absolute.' ;; esac
case $PREFIX in /*) ;; *) fail 'Termux PREFIX must be absolute.' ;; esac
case "$HOME:$PREFIX" in
    *[!A-Za-z0-9_./:-]*) fail 'Use ordinary Termux HOME and PREFIX paths without whitespace or shell metacharacters.' ;;
esac
if [ ! -d "$HOME" ] || [ -L "$HOME" ] || [ ! -x "$PREFIX/bin/sh" ]; then
    fail 'Run this script in a native Termux session.'
fi
case $(uname -m) in aarch64|arm64) ;; *) fail 'This pinned release is for ARM64 phones only.' ;; esac

version=1.104.4
archive=tailscale_${version}_arm64
# Retrieved from https://pkgs.tailscale.com/stable/tailscale_1.104.4_arm64.tgz.sha256
# and checked against the downloaded official tarball before committing this script.
sha256=a1c249ce6bdebef62e1ae3b997595555bce2ae4709bf95a718e9ca7a129dad7e
storage=$HOME/.local/share/cerro-tailscale
release=$storage/bin/$version
state=$storage/tailscaled.state
socket=$storage/tailscaled.sock
login_log=$storage/login.log

for directory in "$HOME/.local" "$HOME/.local/share" "$storage" "$storage/bin"; do
    if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
        fail 'A managed Tailscale directory is not an ordinary directory.'
    fi
    mkdir -p "$directory"
done
chmod 700 "$storage" "$storage/bin"
if [ -L "$release" ] || { [ -e "$release" ] && [ ! -d "$release" ]; }; then
    fail 'The versioned binary path is not an ordinary directory.'
fi
for file in "$state" "$login_log"; do
    if [ -L "$file" ] || { [ -e "$file" ] && [ ! -f "$file" ]; }; then
        fail 'A managed Tailscale state or log path is not a regular file.'
    fi
done
if [ -L "$socket" ] || { [ -e "$socket" ] && [ ! -S "$socket" ]; }; then
    fail 'The Tailscale socket path is not a Unix socket.'
fi

if [ "$action" = install ]; then
    [ ! -e "$release" ] || fail 'This version is already installed; no files were replaced. Use run or status.'
    for tool_name in curl tar sha256sum; do
        command -v "$tool_name" >/dev/null 2>&1 || fail 'Install the prerequisites in Termux: pkg install curl tar coreutils'
    done
    mkdir "$storage/.install-lock" 2>/dev/null || fail 'Another install is active, or an interrupted install left .install-lock. Inspect it before removing the empty lock directory.'
    work=
    trap cleanup_install 0
    trap 'exit 1' 1 2 15
    work=$(mktemp -d "$storage/.install.XXXXXXXX")
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --max-time 300 \
        --output "$work/release.tgz" "https://pkgs.tailscale.com/stable/$archive.tgz"
    printf '%s  %s\n' "$sha256" "$work/release.tgz" | sha256sum -c - >/dev/null 2>&1 || fail 'Official archive checksum mismatch; nothing was installed.'
    # Extract only the two pinned executables, never the systemd service files.
    tar -xzf "$work/release.tgz" -C "$work" "$archive/tailscale" "$archive/tailscaled"
    for name in tailscale tailscaled; do
        if [ ! -f "$work/$archive/$name" ] || [ -L "$work/$archive/$name" ]; then
            fail 'Unexpected binary in the verified archive.'
        fi
        chmod 700 "$work/$archive/$name"
    done
    chmod 700 "$work/$archive"
    mv "$work/$archive" "$release"
    printf 'Installed official Tailscale %s for ARM64. No daemon was started.\n' "$version"
    exit 0
fi

for name in tailscale tailscaled; do
    if [ ! -f "$release/$name" ] || [ -L "$release/$name" ] || [ ! -x "$release/$name" ]; then
        fail 'Run install first; the pinned executables are missing or not ordinary executable files.'
    fi
done
client=$release/tailscale

case $action in
    run)
        # Refuse a pre-existing socket; never unlink a running daemon's endpoint.
        [ ! -e "$socket" ] || fail 'A Tailscale socket already exists. Check status and the original run session before starting another daemon.'
        exec "$release/tailscaled" --tun=userspace-networking \
            --statedir="$storage" --state="$state" --socket="$socket"
        ;;
    login)
        auth_key_file=$2
        case $auth_key_file in /*) ;; *) fail 'Supply an absolute path to a private auth-key file.' ;; esac
        if [ ! -f "$auth_key_file" ] || [ -L "$auth_key_file" ] || [ ! -r "$auth_key_file" ]; then
            fail 'The auth-key path must be a readable regular file, not a symlink.'
        fi
        [ "$(stat -c %u "$auth_key_file")" = "$(id -u)" ] || fail 'The auth-key file must belong to the current Termux user.'
        case $(stat -c %a "$auth_key_file") in 400|600) ;; *) fail 'Keep the auth-key file private: chmod 600 AUTH_KEY_FILE.' ;; esac
        # A failed auth response can be sensitive too. Do not print native output.
        : > "$login_log"
        chmod 600 "$login_log"
        if ! "$client" --socket="$socket" up --accept-dns=false --hostname=cerro \
            --timeout=60s --auth-key="file:$auth_key_file" > "$login_log" 2>&1; then
            fail 'Login failed. Inspect ~/.local/share/cerro-tailscale/login.log locally; do not publish auth keys or login links.'
        fi
        printf '%s\n' 'Tailscale login completed. Keep the run session active; use status to get PHONE_HOST.'
        ;;
    status)
        "$client" --socket="$socket" status
        printf '\nPHONE_HOST='
        "$client" --socket="$socket" ip -4
        ;;
esac
