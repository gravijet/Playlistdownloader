#!/bin/bash
# Refresh yt-dlp and spotDL. YouTube changes its player regularly and an
# outdated yt-dlp is the single most common cause of "nothing downloads".
set -euo pipefail

VENV=/home/benj/Playlistdownloader/.venv
BEFORE=$("$VENV/bin/yt-dlp" --version 2>/dev/null || echo none)

"$VENV/bin/pip" install --quiet --upgrade yt-dlp spotdl

AFTER=$("$VENV/bin/yt-dlp" --version)
if [ "$BEFORE" != "$AFTER" ]; then
    echo "yt-dlp $BEFORE -> $AFTER, restarting service"
    systemctl restart ytdlweb.service
else
    echo "yt-dlp already current ($AFTER)"
fi
