#!/bin/bash
# Container start: make the data folder usable for the dashboard's user, then
# run the dashboard as that user (never as root).
#
# The data folder is mounted from the host. One created by hand (e.g. in
# Synology's File Station) belongs to some other user, and the dashboard,
# running as APP_UID, couldn't save its settings. So, while still root, give
# APP_UID what it can't write yet. Anything it can already write (its own
# files, or a folder open to everyone) is left as it is, so files you edit
# on the host keep their owner. data/mosquitto belongs to the MQTT broker's
# container and is never touched.
set -euo pipefail

APP_UID=1000
DATA=/app/data

as_app() { setpriv --reuid="$APP_UID" --regid="$APP_UID" --clear-groups "$@"; }

if [ "$(id -u)" != "0" ]; then
    exec "$@"   # started as a given user (docker run --user ...): nothing to fix
fi

mkdir -p "$DATA"
while IFS= read -r -d '' path; do
    if ! as_app test -w "$path"; then
        chown "$APP_UID:$APP_UID" "$path"
        echo "docker-entrypoint: $path was not writable for the dashboard (uid $APP_UID); changed its owner"
    fi
done < <(find "$DATA" -path "$DATA/mosquitto" -prune -o ! -user "$APP_UID" -print0)

exec setpriv --reuid="$APP_UID" --regid="$APP_UID" --init-groups "$@"
