#!/bin/bash
# Local end-to-end check of the beetsfs daemon against a real FUSE mount.
# usage: [MUSIC=dir] uv run --reinstall-package beets-beetsfs --project plugin tests/mount_test.sh
# MUSIC defaults to fresh make_fixtures.sh output. It is only read: beets imports a copy of it.
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
bin=$repo/fuse/target/release/beetsfs
TMP=$(mktemp -d)
MNT=$TMP/mnt
SRC=$TMP/src
pid=
cleanup() {
  local rc=$?
  if mountpoint -q "$MNT" 2>/dev/null; then
    fusermount3 -u "$MNT" 2>/dev/null || fusermount3 -u -z "$MNT" || true
  fi
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  if [ "$rc" -ne 0 ] && [ -s "$TMP/daemon.log" ]; then echo "--- daemon log" >&2; tail -n 30 "$TMP/daemon.log" >&2; fi
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
# within <seconds> <command...>: retry the command every 0.1 s
within() { local n=$(($1 * 10)); shift; while ! "$@"; do n=$((n - 1)); [ "$n" -gt 0 ] || return 1; sleep 0.1; done; }
not() { ! "$@"; }

mkdir -p "$MNT"
if [ -z "${MUSIC:-}" ]; then
  "$repo/tests/make_fixtures.sh" "$SRC"
  # a source that is an absolute symlink (music dirs of links into a cloud mount)
  mkdir "$TMP/elsewhere" && cp "$SRC/tagged.flac" "$TMP/elsewhere/linked.flac"
  chmod u+w "$SRC" && ln -s "$TMP/elsewhere/linked.flac" "$SRC/linked.flac" && chmod 0555 "$SRC"
  fixtures=1
else
  cp -r "$MUSIC" "$SRC"
  fixtures=0
fi

export BEETSDIR=$TMP/beets
mkdir -p "$BEETSDIR"
cat > "$BEETSDIR/config.yaml" <<EOF
directory: $MNT
library: $TMP/library.db
import: {copy: no, move: no, write: no}
plugins: [beetsfs]
EOF

py_index() { python -I -c "import pathlib, sqlite3, sys
db = sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True)
$1" "$TMP/beetsfs-index.db"; }
mounted_paths() { (cd "$MNT" && find . -type f -printf '%P\n' | LC_ALL=C sort); }
indexed_paths() { py_index 'for (v,) in db.execute("SELECT vpath FROM files"): sys.stdout.buffer.write(v + b"\n")' | LC_ALL=C sort; }
vpath_of() { py_index "sys.stdout.buffer.write(db.execute('SELECT vpath FROM files WHERE item_id=$1').fetchone()[0])"; }
probe_title() {
  local t
  t=$(ffprobe -v error -show_entries format_tags=title -of default=nw=1:nk=1 "$1")
  [ -n "$t" ] || t=$(ffprobe -v error -select_streams a:0 -show_entries stream_tags=title -of default=nw=1:nk=1 "$1")
  printf '%s' "$t"
}

# 1. daemon starts before the index exists, mounts an empty root
"$bin" "$TMP/beetsfs-index.db" "$MNT" 2>"$TMP/daemon.log" &
pid=$!
within 5 mountpoint -q "$MNT" || fail "not mounted after 5 s"
[ -z "$(ls -A "$MNT")" ] || fail "mount not empty before import"

# 2. import as-is
beet import -A -q "$SRC" >"$TMP/beet.log" 2>&1
[ "$fixtures" = 0 ] || beet modify -y -W -M title:wrong title=Right >>"$TMP/beet.log" 2>&1

# 3. mount content equals library and index
n=$(beet ls | wc -l)
[ "$n" -gt 0 ] || fail "beets imported nothing"
counts_match() { [ "$(find "$MNT" -type f | wc -l)" = "$n" ]; }
paths_match() { [ "$(mounted_paths)" = "$(indexed_paths)" ]; }
within 3 counts_match || fail "mount has $(find "$MNT" -type f | wc -l) files, beets has $n"
within 3 paths_match || fail "mounted paths differ from index vpaths"
declare -A title
while IFS=$'\t' read -r id t; do title[$id]=$t; done < <(beet ls -f $'$id\t$title')
# retagged = has a stored tag blob; the rest are passthrough (original bytes, original tags)
ids=() srcs=() vps=() retagged=()
while IFS= read -r -d '' id && IFS= read -r -d '' r && IFS= read -r -d '' s && IFS= read -r -d '' v; do
  ids+=("$id") retagged+=("$r") srcs+=("$s") vps+=("$v")
done < <(py_index 'for row in db.execute("SELECT item_id, EXISTS (SELECT 1 FROM segments s WHERE s.item_id = f.item_id"
                                     " AND blob_hash IS NOT NULL), src_path, vpath FROM files f"):
    sys.stdout.buffer.write(b"%d\0%d\0%s\0%s\0" % row)')
[ "${#ids[@]}" = "$n" ] || fail "index has ${#ids[@]} rows, beets has $n"

for k in "${!ids[@]}"; do
  s=${srcs[$k]} m=$MNT/${vps[$k]}
  # 4. same decoded audio, tags from beets
  a=$(ffmpeg -v error -i "$s" -map 0:a -f md5 -)
  b=$(ffmpeg -v error -i "$m" -map 0:a -f md5 -)
  [ "$a" = "$b" ] || fail "audio differs: ${vps[$k]}"
  if [ "${retagged[$k]}" = 1 ]; then
    [ "$(probe_title "$m")" = "${title[${ids[$k]}]}" ] || fail "title through mount: ${vps[$k]}: '$(probe_title "$m")' != '${title[${ids[$k]}]}'"
  else
    cmp -s "$s" "$m" || fail "passthrough differs from source: ${vps[$k]}"
  fi
  # 5. stable re-read, last byte readable
  cmp -s <(cat "$m") <(cat "$m") || fail "re-read differs: ${vps[$k]}"
  size=$(stat -c %s "$m")
  [ "$(dd if="$m" bs=1 skip=$((size - 1)) count=1 2>/dev/null | wc -c)" = 1 ] || fail "last byte: ${vps[$k]}"
done

# 6. a tag edit shows up through the mount with a newer mtime
for k in "${!ids[@]}"; do [ "${retagged[$k]}" = 1 ] && break; done
[ "${retagged[$k]}" = 1 ] || fail "no retagged file"
id=${ids[$k]}
old=$(stat -c %Y "$MNT/${vps[$k]}")
sleep 1 # mtime has 1 s resolution
beet modify -y -W -M "id:$id" title=Changed >>"$TMP/beet.log" 2>&1
changed() { local m; m=$MNT/$(vpath_of "$id"); [ "$(probe_title "$m" 2>/dev/null)" = Changed ] && [ "$(stat -c %Y "$m" 2>/dev/null)" -gt "$old" ]; }
within 3 changed || fail "edit not visible through the mount"

# 7. read-only, fixed modes
not touch "$MNT/x" 2>/dev/null || fail "touch succeeded"
m=$MNT/$(vpath_of "$id")
not sh -c 'echo >> "$1"' sh "$m" 2>/dev/null || fail "append succeeded"
[ "$(find "$MNT" -type f -exec stat -c %a {} + | sort -u)" = 444 ] || fail "file mode not 444"
[ "$(find "$MNT" -type d -exec stat -c %a {} + | sort -u)" = 555 ] || fail "dir mode not 555"

# 8. a vanished source fails that file only
k=$((${#ids[@]} - 1))
chmod u+w "$(dirname "${srcs[$k]}")"
mv "${srcs[$k]}" "$TMP/moved-away"
not cat "$MNT/${vps[$k]}" >/dev/null 2>&1 || fail "cat of a vanished source succeeded"
ls "$MNT" >/dev/null || fail "ls failed after source vanished"
# a source changed since indexing is served as its original bytes (metadata only: touch).
# st_size still says the indexed size until the next reconcile, so compare a cat, not the files.
touch "${srcs[0]}"
cat "$MNT/$(vpath_of "${ids[0]}")" | cmp -s - "${srcs[0]}" || fail "changed source not served as original"

# 9. SIGTERM removes the mount and the daemon exits 0, even while a file in the view is open
exec 3<"$m"
kill "$pid"
within 3 not mountpoint -q "$MNT" || fail "still mounted 3 s after kill"
wait "$pid" || fail "daemon exited $? after SIGTERM"
exec 3<&-
pid=
echo "OK ($n files)"
