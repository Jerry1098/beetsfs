#!/bin/bash
# usage: make_fixtures.sh <outdir>
set -euo pipefail
out=$(mkdir -p "$1" && cd "$1" && pwd)
w=$(mktemp -d); trap 'rm -rf "$w"' EXIT
ff() { ffmpeg -v error -y "$@"; }
ff -f lavfi -i mandelbrot=s=600x600 -frames:v 1 "$w/cover.png"
[ "$(stat -c %s "$w/cover.png")" -gt 20000 ] || { echo "cover too small" >&2; exit 1; }
ff -f lavfi -i sine=d=3 -i "$w/cover.png" -map 0 -map 1 -c:a libmp3lame -c:v copy \
  -id3v2_version 3 -write_id3v1 1 -metadata title=wrong "$out/tagged.mp3"
ff -f lavfi -i sine=d=3 -c:a libmp3lame -id3v2_version 0 -write_id3v1 0 "$out/bare.mp3"
ff -f lavfi -i sine=d=3 -i "$w/cover.png" -map 0 -map 1 -c:a flac -c:v copy \
  -disposition:v attached_pic -metadata title=wrong "$out/tagged.flac"
ff -f lavfi -i sine=d=3 -c:a libvorbis "$out/plain.ogg"
python3 -c '
import sys
b = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(b"ID3\x04\x00\x00\x00\x00\x00\x04abcd" + b)' "$out/tagged.flac" "$out/id3.flac"
{ printf 'fLaC'; head -c 20 /dev/urandom; } > "$out/garbage.flac"
: > "$out/empty.mp3"
chmod 0444 "$out"/*
chmod 0555 "$out"
