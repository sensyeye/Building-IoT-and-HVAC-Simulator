#!/bin/sh
# Entry point for the Building IoT & HVAC Simulator container.
#
# Bind-mounted volumes (data/, outputs/) arrive owned by the host user
# (often root), which prevents the non-root `app` user inside the
# container from writing to them. We start as root, fix ownership of
# the writable directories, then drop privileges to `app`.
#
# This is a no-op for named/anonymous volumes — they were already
# chowned during image build.

set -e

APP_USER="${APP_USER:-app}"
APP_GROUP="${APP_GROUP:-app}"

# Directories the application needs to write to at runtime.
WRITABLE_DIRS="/app/data /app/outputs"

if [ "$(id -u)" = "0" ]; then
    for dir in $WRITABLE_DIRS; do
        mkdir -p "$dir"
        # Only chown if not already owned by app — avoids slow recursive
        # chown on large outputs/ folders on every restart.
        if [ "$(stat -c '%U' "$dir" 2>/dev/null)" != "$APP_USER" ]; then
            chown -R "${APP_USER}:${APP_GROUP}" "$dir" || true
        fi
    done

    # Drop privileges and exec the original CMD.
    exec gosu "${APP_USER}:${APP_GROUP}" "$@"
fi

# Already non-root: just exec.
exec "$@"
