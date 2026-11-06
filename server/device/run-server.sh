#!/system/bin/sh
# Native CPU baseline. No root, system properties, init edits, or implicit downloads.
set -eu
umask 077

ROOT=${CERRO_SERVER_DIR:-/data/local/tmp/cerro-server}
BIN=$ROOT/bin/llama-server
PIDFILE=$ROOT/server.pid
LOCK=$ROOT/.control-lock

fail() { printf '%s\n' "$*" >&2; exit 1; }

start_ticks() {
    [ -r "/proc/$1/stat" ] || return 1
    line=$(cat "/proc/$1/stat") || return 1
    # The comm field may contain spaces or parentheses; the last ') ' ends it.
    rest=${line##*) }
    set -f
    # Intentional splitting of numeric /proc fields, with pathname expansion disabled.
    # shellcheck disable=SC2086
    set -- $rest
    [ "$#" -ge 20 ] || return 1
    shift 19
    printf '%s\n' "$1"
}

server_pid() {
    [ -r "$PIDFILE" ] || return 1
    IFS=' ' read -r pid ticks extra < "$PIDFILE" || return 1
    case $pid in ''|*[!0-9]*) return 1 ;; esac
    case $ticks in ''|*[!0-9]*) return 1 ;; esac
    [ -z "$extra" ] && [ "$pid" -gt 1 ] || return 1
    [ "$(start_ticks "$pid" 2>/dev/null)" = "$ticks" ] || return 1
    executable=$(readlink "/proc/$pid/exe" 2>/dev/null) || return 1
    expected=$(readlink -f "$BIN") || return 1
    [ "$executable" = "$expected" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

lock() {
    [ ! -L "$ROOT" ] || fail 'Server directory must not be a symlink.'
    mkdir -p "$ROOT"
    if ! mkdir "$LOCK" 2>/dev/null; then
        fail 'Another control operation is active, or an interrupted operation left .control-lock. Verify no control operation is running before removing that empty directory.'
    fi
    trap 'rmdir "$LOCK" 2>/dev/null || true' 0
    trap 'exit 1' 1 2 15
}

case ${1:-} in
    status)
        if server_pid >/dev/null; then
            printf '%s\n' 'Server process is running. Use probe to test readiness and inference.'
        else
            printf '%s\n' 'Server process is stopped.'
            exit 3
        fi
        ;;
    start)
        lock
        [ ! -f "$ROOT/.deploying" ] || fail 'An interrupted installation must be repaired with deploy before starting.'
        if server_pid >/dev/null; then
            printf '%s\n' 'Server is already running.'
            exit 0
        fi
        [ -x "$BIN" ] || fail 'Server binary is missing; deploy the kit first.'
        [ -s "$ROOT/models/model.gguf" ] || fail 'A local GGUF model is required.'
        [ -f "$ROOT/config/api-key" ] || fail 'API key file is missing.'
        key=$(cat "$ROOT/config/api-key")
        [ "${#key}" -eq 64 ] || fail 'API key must have 64 hexadecimal characters.'
        case $key in *[!0-9a-f]*) fail 'API key must have 64 hexadecimal characters.' ;; esac
        unset key
        chmod 600 "$ROOT/config/api-key"
        rm -f "$PIDFILE"
        nohup "$BIN" \
            --model "$ROOT/models/model.gguf" \
            --host 127.0.0.1 --port 8080 \
            --api-key-file "$ROOT/config/api-key" \
            --n-gpu-layers 0 --ctx-size 2048 --parallel 1 --threads 4 \
            --offline --no-ui \
            > "$ROOT/server.log" 2>&1 < /dev/null &
        child=$!
        # Wait for nohup to exec the actual server before recording its identity.
        attempts=0
        while [ "$attempts" -lt 50 ]; do
            ticks=$(start_ticks "$child" 2>/dev/null) || break
            executable=$(readlink "/proc/$child/exe" 2>/dev/null) || break
            if [ "$executable" = "$(readlink -f "$BIN")" ]; then
                printf '%s %s\n' "$child" "$ticks" > "$PIDFILE"
                printf '%s\n' 'Server started on 127.0.0.1:8080. Model loading may take time; run probe.'
                exit 0
            fi
            attempts=$((attempts + 1))
            sleep 0.1
        done
        fail 'Server did not start; inspect server.log on the device.'
        ;;
    stop)
        lock
        if pid=$(server_pid); then
            # PID + kernel start time + executable are checked, not just a stale PID file.
            kill "$pid" || fail 'Could not request server shutdown.'
            attempts=0
            while server_pid >/dev/null && [ "$attempts" -lt 50 ]; do
                attempts=$((attempts + 1))
                sleep 0.1
            done
            if server_pid >/dev/null; then
                fail 'Server has not stopped yet; retry status. No forced kill was sent.'
            fi
        fi
        rm -f "$PIDFILE"
        printf '%s\n' 'Server stopped.'
        ;;
    install)
        stage=${2:-}
        suffix=${stage#"$ROOT/.stage-"}
        if [ "$stage" = "$suffix" ] || [ "${#suffix}" -ne 32 ]; then
            fail 'Invalid staging directory.'
        fi
        case $suffix in *[!0-9a-f]*) fail 'Invalid staging directory.' ;; esac
        if [ ! -d "$stage" ] || [ -L "$stage" ]; then
            fail 'Staging directory is missing.'
        fi
        lock
        if server_pid >/dev/null; then
            fail 'Stop the running server before deploying another kit or model.'
        fi
        for file in bin/llama-server device/run-server.sh provenance.json models/model.gguf config/api-key; do
            if [ ! -f "$stage/$file" ] || [ -L "$stage/$file" ]; then
                fail 'Staged payload is incomplete.'
            fi
        done
        (cd "$stage" && sha256sum -c upload.sha256 >/dev/null) || fail 'Transferred payload checksum failed.'
        key=$(cat "$stage/config/api-key")
        [ "${#key}" -eq 64 ] || fail 'Staged API key is invalid.'
        case $key in *[!0-9a-f]*) fail 'Staged API key is invalid.' ;; esac
        unset key
        # A failed or interrupted commit stays unstartable until a complete redeploy.
        : > "$ROOT/.deploying"
        mkdir -p "$ROOT/bin" "$ROOT/device" "$ROOT/config" "$ROOT/models"
        chmod 700 "$ROOT" "$ROOT/bin" "$ROOT/device" "$ROOT/config" "$ROOT/models"
        for file in bin/llama-server device/run-server.sh provenance.json models/model.gguf config/api-key; do
            mv -f "$stage/$file" "$ROOT/$file"
        done
        if [ -f "$stage/bin/llama-bench" ]; then
            mv -f "$stage/bin/llama-bench" "$ROOT/bin/llama-bench"
            chmod 700 "$ROOT/bin/llama-bench"
        else
            rm -f "$ROOT/bin/llama-bench"
        fi
        chmod 700 "$BIN" "$ROOT/device/run-server.sh"
        chmod 600 "$ROOT/config/api-key" "$ROOT/models/model.gguf" "$ROOT/provenance.json"
        rm -f "$PIDFILE" "$ROOT/.deploying"
        printf '%s\n' 'Kit installed. Start it explicitly; installation does not change boot behavior.'
        ;;
    *)
        fail 'Usage: run-server.sh start|stop|status'
        ;;
esac
