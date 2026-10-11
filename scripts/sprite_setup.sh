#!/bin/bash
# One-time (and safe to re-run) provisioning of a Fly.io sprite for the
# weekly run. Run it on the sprite from the repo checkout:
#
#   sprite exec -s sermon-shorts -- bash -lc 'git clone https://github.com/pshouse/sermon-shorts.git; sermon-shorts/scripts/sprite_setup.sh'
#
# Then copy the secrets in from a machine that has them:
#   for f in .env church.json youtube_client_secret.json .youtube_token.json; do
#     sprite file push -s sermon-shorts "$f" "/home/sprite/sermon-shorts/$f"; done
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# OpenCV's wheel links against X/GL libraries the base image lacks. The
# sprite also ships with no fonts at all, and libass then renders captions
# and the cover title as *nothing*, silently — Arial Black (the font the ASS
# styles name) comes from the Microsoft core fonts package.
echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
    | sudo debconf-set-selections
sudo apt-get -qq update
sudo DEBIAN_FRONTEND=noninteractive apt-get -qq install -y \
    libxcb1 libgl1 libglib2.0-0 fontconfig ttf-mscorefonts-installer caddy >/dev/null
fc-list | grep -qi "arial black" || { echo "Arial Black did not install"; exit 1; }

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# Pre-cache the whisper model: sprite_weekly.sh runs with HF_HUB_OFFLINE=1
# because the shared egress IP gets rate-limited by the Hugging Face Hub.
for i in $(seq 1 10); do
    .venv/bin/python -c 'from faster_whisper.utils import download_model; download_model("medium")' && break
    echo "model download failed (attempt $i), retrying in 30s"; sleep 30
done

mkdir -p "$HOME/Downloads" "$HOME/logs"
chmod +x scripts/*.sh

# Read-only review page for the rendered clips, behind the sprite URL's org
# auth. Caddy rather than python -m http.server: iOS won't play an MP4
# without byte-range support.
sprite-env services list | grep -q '"name":"clips"' || sprite-env services create clips \
    --cmd /usr/bin/caddy --args "file-server,--root,$HOME/Downloads,--browse,--listen,:8080" \
    --http-port 8080 --no-stream
echo "sprite ready"
