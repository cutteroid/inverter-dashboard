#!/bin/bash
# Update the dashboard on a NAS / server that runs it with Docker (e.g. Synology):
# sends only the files the image is built from (~2 MB) over SSH, then rebuilds
# and restarts there. Docker reuses its cached layers, so a code-only update
# takes seconds; only the first run (base image + Python packages) is slow.
#
#   tools/deploy_nas.sh            deploy (asks for the NAS address on first run;
#                                  then at most one password prompt per run)
#   tools/deploy_nas.sh --setup    change the saved NAS address / folder
#
# Settings are kept in tools/.deploy_nas.env (not committed; DEPLOY_NAS_CONF=path
# overrides it). The NAS folder's
# data/ (config, stats, broker files) is never touched.
set -euo pipefail
cd "$(dirname "$0")/.."

# DEPLOY_NAS_CONF: use another settings file (e.g. for tests; never touches yours)
CONF=${DEPLOY_NAS_CONF:-tools/.deploy_nas.env}
if [ "${1:-}" = "--setup" ] || [ ! -f "$CONF" ]; then
    # shellcheck disable=SC1090
    [ -f "$CONF" ] && . "$CONF"
    echo "NAS deployment settings (saved to $CONF)"
    read -rp "  SSH login, user@host [${NAS_SSH:-}]: " v; NAS_SSH=${v:-${NAS_SSH:-}}
    read -rp "  SSH port [${NAS_PORT:-22}]: " v; NAS_PORT=${v:-${NAS_PORT:-22}}
    read -rp "  Dashboard folder on the NAS [${NAS_DIR:-/volume1/docker/extended-dashboard-arm64}]: " v
    NAS_DIR=${v:-${NAS_DIR:-/volume1/docker/extended-dashboard-arm64}}
    [ -n "$NAS_SSH" ] && [ -n "$NAS_DIR" ] || { echo "SSH login and folder are required" >&2; exit 1; }
    printf 'NAS_SSH=%q\nNAS_PORT=%q\nNAS_DIR=%q\n' "$NAS_SSH" "$NAS_PORT" "$NAS_DIR" > "$CONF"
    [ "${1:-}" = "--setup" ] && exit 0
fi
# shellcheck disable=SC1090
. "$CONF"
case "$NAS_DIR" in /*/*) ;; *) echo "NAS_DIR must be an absolute path like /volume1/docker/..." >&2; exit 1 ;; esac

# One SSH connection for all steps, opened first; every later step reuses it.
# Login: an SSH key if there is one (ssh-copy-id user@nas), otherwise the NAS
# password, asked once here and reused for sudo below (on DSM it's the same).
SOCKET_DIR=$(mktemp -d)
trap 'ssh -p "$NAS_PORT" -o ControlPath="$SOCKET_DIR/s" -O exit "$NAS_SSH" 2>/dev/null; rm -rf "$SOCKET_DIR"' EXIT
SSH=(ssh -p "$NAS_PORT" -o ConnectTimeout=10 -o ControlPath="$SOCKET_DIR/s")
# accept-new: a NAS seen for the first time is remembered (instead of a yes/no
# question the password helper would answer); a changed host key is still refused
MASTER=("${SSH[@]}" -fN -o ControlMaster=yes -o ControlPersist=300 -o StrictHostKeyChecking=accept-new)
echo "==> Connecting to $NAS_SSH"
NAS_PW=""
if ! "${MASTER[@]}" -o BatchMode=yes "$NAS_SSH" 2>/dev/null; then
    read -rsp "    NAS password for $NAS_SSH: " NAS_PW; echo
    # OpenSSH takes the password from this helper instead of the keyboard
    # (SSH_ASKPASS_REQUIRE needs OpenSSH 8.4+; older versions just ask themselves)
    ASKPASS="$SOCKET_DIR/askpass"
    printf '#!/bin/sh\nprintf "%%s\\n" "$NAS_DEPLOY_PW"\n' > "$ASKPASS"
    chmod 700 "$ASKPASS"
    NAS_DEPLOY_PW="$NAS_PW" SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force \
        "${MASTER[@]}" -o NumberOfPasswordPrompts=1 "$NAS_SSH" ||
        { echo "    Login failed" >&2; exit 1; }
fi

# sudo password: none needed, else the login password, else asked (up to 3 tries).
# It reaches the NAS over the SSH connection's stdin (never on a command line,
# where `ps` would show it).
sudo_ok() { printf '%s\n' "$1" | "${SSH[@]}" "$NAS_SSH" "sudo -S -p '' -v" 2>/dev/null; }
SUDO_PW=""
if ! "${SSH[@]}" -n "$NAS_SSH" "sudo -n true" 2>/dev/null; then
    if [ -n "$NAS_PW" ] && sudo_ok "$NAS_PW"; then
        SUDO_PW=$NAS_PW
    else
        for attempt in 1 2 3; do
            read -rsp "    NAS password for sudo ($NAS_SSH): " SUDO_PW; echo
            sudo_ok "$SUDO_PW" && break
            echo "    Wrong password" >&2
            [ "$attempt" = 3 ] && exit 1
        done
    fi
fi
unset NAS_PW

# What the image is built from: Dockerfile COPY sources + compose + broker entrypoint
FILES=(Dockerfile .dockerignore docker-compose.yml mosquitto/entrypoint.sh)
while read -r src; do FILES+=("$src"); done < <(sed -n 's/^COPY \([^ ]*\) .*/\1/p' Dockerfile)
DIRS=()
for f in "${FILES[@]}"; do
    [ -e "$f" ] || { echo "Missing $f (listed in Dockerfile)" >&2; exit 1; }
    [ -d "$f" ] && DIRS+=("${f%/}")
done

# macOS tar (bsdtar) would add Apple metadata (._ files, xattrs such as
# com.apple.provenance) that GNU tar on the NAS warns about for every file
TAR_FLAGS=()
tar --version 2>/dev/null | grep -q bsdtar && TAR_FLAGS=(--no-xattrs --no-mac-metadata --no-acls)
pack() {
    COPYFILE_DISABLE=1 tar ${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"} -czf - --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' "${FILES[@]}"
}

echo "==> Sending $(pack | wc -c | awk '{printf "%.1f MB", $1/1048576}') to $NAS_SSH:$NAS_DIR"
# Directories are replaced as a whole so files deleted here don't linger there
pack |
    "${SSH[@]}" "$NAS_SSH" "set -e; mkdir -p '$NAS_DIR'; cd '$NAS_DIR'; rm -rf ${DIRS[*]}; mkdir -p data/mosquitto; tar -xzf -"

# Build progress on the NAS: compose logs each step without a terminal
# (BuildKit "#13 [svc 10/20] RUN ..." then "#13 DONE 2.1s" / "#13 CACHED";
# classic builder "Step 10/20 : RUN ..."). This prints one line per step as it
# starts, with the share of steps already finished and what the step does, e.g.
# "  25%  [5/20] Python packages (pip)", plus how long a slow step took. So a
# hanging download shows which step it's in. (One awk program on one line, so
# no awk comments in it; POSIX awk only: it runs in the NAS's awk.)
PROGRESS_AWK='function label(s,  w) {
    if (s ~ /^FROM/) return "base image";
    if (s ~ /apt-get/) return "system packages (apt-get)";
    if (s ~ /pip install/) return "Python packages (pip)";
    if (s ~ /useradd/) return "user and data folder";
    if (s ~ /^WORKDIR/) return "working folder";
    if (s ~ /^COPY/) { split(s, w, " "); return "copy " w[2] }
    return substr(s, 1, 60)
}
function show(n, total, s) { printf "%4d%%  [%d/%d] %s\n", int(100 * (n - 1) / total), n, total, label(s); fflush() }
/^Step [0-9]+\/[0-9]+ :/ { split($2, a, "/"); t = $0; sub(/^Step [^:]*: */, "", t); show(a[1], a[2], t); next }
$1 ~ /^#[0-9]+$/ && !($1 in steps) && match($0, /[0-9]+\/[0-9]+\] /) {
    split(substr($0, RSTART, RLENGTH - 2), a, "/"); steps[$1] = 1; t = substr($0, RSTART + RLENGTH); show(a[1], a[2], t); next
}
$2 == "DONE" && ($1 in steps) { d = $3; sub(/s$/, "", d); if (d + 0 >= 20) { printf "        took %d s\n", d + 0; fflush() } next }
/naming to .* done|Successfully tagged|Image .* Built/ { finished = 1 }
END { if (finished) print " 100%  done" }'
PROGRESS_AWK=$(printf '%s' "$PROGRESS_AWK" | tr '\n' ' ')

echo "==> Rebuilding and restarting on the NAS"
# The first stdin line is the sudo password (empty: sudo needs none); every sudo
# call gets it from there, so nothing prompts again.
# Synology: docker lives in /usr/local/bin, which sudo's PATH may not include;
# older DSM only has the standalone docker-compose
printf '%s\n' "$SUDO_PW" | "${SSH[@]}" "$NAS_SSH" "set -e; cd '$NAS_DIR'
    IFS= read -r PW
    s() { if [ -n \"\$PW\" ]; then printf '%s\\n' \"\$PW\" | sudo -S -p '' \"\$@\"; else sudo -n \"\$@\"; fi; }
    DOCKER=\$(command -v docker || echo /usr/local/bin/docker)
    if s \"\$DOCKER\" compose version >/dev/null 2>&1; then DC=\"\$DOCKER compose\"
    else DC=\$(command -v docker-compose || echo /usr/local/bin/docker-compose); fi
    # Reuse the compose project of the running containers (Container Manager names
    # it after the Project, not the folder); otherwise compose would try to create
    # a second set with the same container names. Older versions named the containers
    # deye-dashboard / deye-mosquitto: looked up under those names too.
    PROJECT=
    for c in inverter-dashboard deye-dashboard; do
        PROJECT=\$(s \"\$DOCKER\" inspect -f '{{index .Config.Labels \"com.docker.compose.project\"}}' \"\$c\" 2>/dev/null || true)
        case \"\$PROJECT\" in ''|'<no value>') PROJECT= ;; *) break ;; esac
    done
    case \"\$PROJECT\" in ''|'<no value>') PROJECT=\$(basename \"\$PWD\") ;; esac
    echo \"Compose project: \$PROJECT\"
    # No terminal on the NAS side (stdin carries the password): show the build as
    # a line per step (progress and what it does); the full log is kept and printed if the build fails
    echo 'Building image (seconds; first run or new requirements: several minutes)...'
    LOG=\$(mktemp)
    { s \$DC -p \"\$PROJECT\" build 2>&1; echo \"__EXIT__\$?\"; } | tee \"\$LOG\" | awk $(printf '%q' "$PROGRESS_AWK")
    if ! grep -q '^__EXIT__0\$' \"\$LOG\"; then
        echo 'Build failed:'; grep -v '^__EXIT__' \"\$LOG\" | tail -n 40; rm -f \"\$LOG\"; exit 1
    fi
    rm -f \"\$LOG\"
    # Renamed containers (deye-* -> inverter-*): remove the old ones first, they hold
    # ports 8080 / 1883 (the data stays in data/)
    for old in deye-dashboard deye-mosquitto; do
        if s \"\$DOCKER\" inspect \"\$old\" >/dev/null 2>&1; then
            echo \"Replacing the old container \$old\"; s \"\$DOCKER\" rm -f \"\$old\" >/dev/null
        fi
    done
    s \$DC -p \"\$PROJECT\" up -d --remove-orphans
    s \"\$DOCKER\" image prune -f >/dev/null   # drop the replaced image
    s \$DC -p \"\$PROJECT\" ps"
unset SUDO_PW
echo "==> Done: http://${NAS_SSH#*@}:8080"
