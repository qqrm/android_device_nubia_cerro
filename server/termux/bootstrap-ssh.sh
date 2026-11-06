#!/data/data/com.termux/files/usr/bin/sh
# Run once inside Termux. The only input is an Ed25519 PUBLIC deployment key.
set -eu
umask 077

fail() { printf '%s\n' "$*" >&2; exit 1; }

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
    fail 'Usage: sh bootstrap-ssh.sh PUBLIC_KEY_FILE [--wake-lock]'
fi
public_key=$1
wake_lock=0
if [ "$#" -eq 2 ]; then
    [ "$2" = '--wake-lock' ] || fail 'The only optional argument is --wake-lock.'
    wake_lock=1
fi
[ -f "$public_key" ] || fail 'Supply the deployment public key (.pub), not a private key.'
if [ -z "${PREFIX:-}" ] || [ -z "${HOME:-}" ]; then
    fail 'Run this script inside Termux.'
fi
case $HOME in /*) ;; *) fail 'Termux HOME must be an absolute path.' ;; esac
case $PREFIX in /*) ;; *) fail 'Termux PREFIX must be an absolute path.' ;; esac
# These paths are also written into sshd_config and the boot-script shebang.
case "$HOME:$PREFIX" in
    *[!A-Za-z0-9_./:-]*) fail 'HOME and PREFIX must use ordinary Termux paths without whitespace or shell metacharacters.' ;;
esac
for tool_name in sh sshd ssh-keygen ssh-keyscan python; do
    [ -x "$PREFIX/bin/$tool_name" ] || fail 'Install the prerequisites in Termux: pkg install openssh python'
done
"$PREFIX/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail 'Python 3.10 or later is required.'
if [ "$wake_lock" -eq 1 ]; then
    [ -x "$PREFIX/bin/termux-wake-lock" ] || fail 'termux-wake-lock is missing; update the termux-tools package first.'
fi

work=$(mktemp -d "$HOME/.cerro-ssh-setup.XXXXXXXX")
setup_lock=
cleanup() {
    rm -rf "$work"
    if [ -n "$setup_lock" ]; then rmdir "$setup_lock" 2>/dev/null || true; fi
}
trap cleanup 0
trap 'exit 1' 1 2 15

# Accept a single ordinary .pub line, including one copied from Windows.
tr -d '\r' < "$public_key" > "$work/public-key"
[ "$(awk 'END { print NR }' "$work/public-key")" = 1 ] || fail 'Supply exactly one Ed25519 public-key line.'
key_type='' key_data='' _key_comment=''
IFS=' ' read -r key_type key_data _key_comment < "$work/public-key" || true
[ "$key_type" = ssh-ed25519 ] || fail 'Supply an Ed25519 public key created with ssh-keygen -t ed25519.'
case $key_data in ''|*[!A-Za-z0-9+/=]*) fail 'Invalid public-key encoding.' ;; esac
"$PREFIX/bin/ssh-keygen" -l -f "$work/public-key" >/dev/null 2>&1 || fail 'Invalid Ed25519 public key.'
unset _key_comment

state=$HOME/.ssh/cerro-sshd
for directory in "$HOME/.ssh" "$state" "$HOME/.termux" "$HOME/.termux/boot"; do
    [ ! -L "$directory" ] || fail 'SSH and boot directories must not be symlinks.'
    mkdir -p "$directory"
done
chmod 700 "$HOME/.ssh" "$state"
if ! mkdir "$state/.setup-lock" 2>/dev/null; then
    fail 'Another bootstrap is active, or .ssh/cerro-sshd/.setup-lock remains after interruption. Check before removing the empty lock directory.'
fi
setup_lock=$state/.setup-lock
configuration=$state/sshd_config
authorized=$state/authorized_keys
host_key=$state/ssh_host_ed25519_key
boot=$HOME/.termux/boot/20-cerro-sshd
for file in "$configuration" "$authorized" "$host_key" "$host_key.pub" "$boot" "$state/keep-awake" "$state/sshd.pid" "$state/sshd.log"; do
    if [ -L "$file" ] || { [ -e "$file" ] && [ ! -f "$file" ]; }; then
        fail 'A managed SSH or boot path is not a regular file; no existing file was replaced.'
    fi
done
if [ ! -f "$host_key" ]; then
    [ ! -e "$host_key.pub" ] || fail 'A host public key already exists without its private key; restore the matching key pair first.'
    "$PREFIX/bin/ssh-keygen" -q -t ed25519 -N '' -C cerro-phone -f "$host_key"
fi
chmod 600 "$host_key"
host_public=$("$PREFIX/bin/ssh-keygen" -y -P '' -f "$host_key") || fail 'The existing host key could not be read; it was not replaced.'
host_public=$(printf '%s\n' "$host_public" | awk '{ print $1 " " $2 }')
user_name=$(id -un)
case $user_name in ''|*[!A-Za-z0-9_-]*) fail 'Unexpected Termux user name.' ;; esac

cat > "$work/sshd_config" <<EOF
# Managed by cerro bootstrap-ssh.sh; separate from the normal Termux SSH config.
Port 8022
ListenAddress 0.0.0.0
ListenAddress ::
HostKey "$host_key"
PidFile "$state/sshd.pid"
AuthorizedKeysFile "$authorized"
AllowUsers $user_name
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin no
StrictModes yes
AllowAgentForwarding no
X11Forwarding no
PermitTTY no
PermitUserRC no
PermitUserEnvironment no
AllowTcpForwarding local
AllowStreamLocalForwarding no
PermitOpen 127.0.0.1:8080
GatewayPorts no
PermitTunnel no
Subsystem sftp internal-sftp
LogLevel ERROR
EOF

printf '#!%s/bin/sh\n' "$PREFIX" > "$work/boot"
cat >> "$work/boot" <<'BOOT'
# Managed by cerro bootstrap-ssh.sh. Starts SSH only, not the model server.
set -eu
umask 077
state=$HOME/.ssh/cerro-sshd
configuration=$state/sshd_config
if [ -f "$state/keep-awake" ]; then "$PREFIX/bin/termux-wake-lock"; fi
"$PREFIX/bin/sshd" -t -f "$configuration"
# Compare the loopback listener with the host key generated on this phone.
# This scan is only for idempotency; clients still pin the printed host key.
expected=$("$PREFIX/bin/ssh-keygen" -y -P '' -f "$state/ssh_host_ed25519_key")
expected=$(printf '%s\n' "$expected" | awk '{ print $1 " " $2 }')
observed=$("$PREFIX/bin/ssh-keyscan" -T 2 -p 8022 -t ed25519 127.0.0.1 2>/dev/null | awk '$2 == "ssh-ed25519" { print $2 " " $3; exit }')
if [ "$observed" = "$expected" ]; then exit 0; fi
if ! "$PREFIX/bin/sshd" -f "$configuration" -E "$state/sshd.log"; then
    printf '%s\n' 'Could not start cerro SSH on port 8022. If another sshd uses this port, stop that service explicitly and rerun the bootstrap. See ~/.ssh/cerro-sshd/sshd.log.' >&2
    exit 1
fi
BOOT

# An existing custom configuration/boot helper is never silently overwritten.
for name in sshd_config boot; do
    destination=$configuration
    if [ "$name" = boot ]; then destination=$boot; fi
    if [ -f "$destination" ] && [ "$(cat "$destination")" != "$(cat "$work/$name")" ]; then
        fail 'An existing cerro SSH config or boot helper differs. Review it and move it aside explicitly before rerunning; no existing config was replaced.'
    fi
done
"$PREFIX/bin/sshd" -t -f "$work/sshd_config" || fail 'OpenSSH rejected the configuration; no daemon was started.'
if [ ! -f "$configuration" ]; then cp "$work/sshd_config" "$configuration"; fi
if [ ! -f "$boot" ]; then cp "$work/boot" "$boot"; fi

# Keep existing dedicated deployment keys. Do not touch ~/.ssh/authorized_keys.
touch "$authorized"
if ! awk -v key="$key_data" '{ for (i = 1; i < NF; i++) if ($i == "ssh-ed25519" && $(i + 1) == key) found = 1 } END { exit !found }' "$authorized"; then
    printf '\nrestrict,port-forwarding,permitopen="127.0.0.1:8080" %s %s cerro-github-deploy\n' "$key_type" "$key_data" >> "$authorized"
fi
chmod 600 "$configuration" "$authorized"
chmod 700 "$boot"
# These paths are needed for the first model upload; keep existing models/keys.
for directory in "$HOME/cerro-server" "$HOME/cerro-server/models" "$HOME/cerro-server/config"; do
    if [ -L "$directory" ] || { [ -e "$directory" ] && [ ! -d "$directory" ]; }; then
        fail 'The cerro-server, models and config paths must be ordinary directories.'
    fi
    mkdir -p "$directory"
    chmod 700 "$directory"
done
if [ "$wake_lock" -eq 1 ]; then touch "$state/keep-awake"; fi
"$PREFIX/bin/sh" "$boot"

printf '\n%s\n' 'Cerro SSH is ready on port 8022 (public-key authentication only).'
printf 'PHONE_USER=%s\n' "$user_name"
printf '%s\n' 'PHONE_KNOWN_HOSTS (copy this public line from this trusted terminal):'
printf 'cerro-phone %s\n' "$host_public"
printf '%s\n' 'Install Termux:Boot from the same source as Termux and open it once for SSH startup after reboot.'
printf '%s\n' 'Connect the Android Tailscale app separately; use its phone IP/name as PHONE_HOST.'
printf '%s\n' 'The boot helper starts SSH only. Model-server startup is a separate deployment action.'
if [ -f "$state/keep-awake" ]; then
    printf '%s\n' 'Wake lock is enabled. To disable: remove ~/.ssh/cerro-sshd/keep-awake and run termux-wake-unlock.'
fi
