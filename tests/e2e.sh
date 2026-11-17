#!/bin/bash
# Container plumbing check on podman: build, start with compose, import generated fixtures
# (plus absolute symlinks into a FUSE mount, the cloud case), check the view inside the
# container, from a second (consumer) container, and, rootful, on the host. Both run confined
# (SELinux container_t). File contents are tests/mount_test.sh's job.
# usage: tests/e2e.sh (rootless) or sudo tests/e2e.sh (rootful)
# needs podman, podman-compose, ffmpeg, fuse-overlayfs; no network for beets: import -A
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
export MUSIC=$TMP/music CONFIG=$TMP/config VIEW=$TMP/view LINKS_ROOT=$TMP/links PODMAN_COMPOSE_WARNING_LOGS=false
root=; [ "$(id -u)" != 0 ] || root=1
# mount namespace work: the host's when rootful, podman's user namespace when rootless
ns() { if [ -n "$root" ]; then "$@"; else podman unshare "$@"; fi; }
proj=beetsfs-e2e-$$ holder=beetsfs-e2e-holder-$$
c() { podman compose -p "$proj" -f "$repo/compose.yaml" -f "$TMP/test.yaml" "$@"; }
box() { c exec -T beetsfs "$@" </dev/null; }                 # root in the container, like everything there
# the image has no shell or coreutils: in-container probes are python (args: sys.argv[1:])
py() { local code=$1; shift; box python -I -c "$code" "$@"; }
nfiles() { py 'import os, sys; print(sum(len(f) for _, _, f in os.walk(sys.argv[1])))' "$1"; }
md5() { py 'import hashlib, sys; print(hashlib.md5(open(sys.argv[1], "rb").read()).hexdigest())' "$1"; }
# open(path, "w") fails with EROFS: not any failure (a failed exec would pass a plain negation)
rofs() { py 'import errno, sys
try: open(sys.argv[1], "w")
except OSError as e: sys.exit(e.errno != errno.EROFS and str(e))
else: sys.exit("writable")' "$1"; }
killd() { py 'import os, sys
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        if open(f"/proc/{p}/comm").read() == "beetsfs\n": os.kill(int(p), int(sys.argv[1]))
    except OSError: pass' "$1"; }
V=/beetsfs/music CV=/view/music   # the view inside beetsfs, and in a consumer binding $VIEW at /view
consumer() { podman run --rm -v "$VIEW:/view:ro,rslave" docker.io/library/alpine:3.20 "$@"; }
state() { podman ps -a --filter "label=io.podman.compose.project=$proj" --format '{{.State}}'; }
exitcode() { podman ps -a --filter "label=io.podman.compose.project=$proj" --format '{{.ExitCode}}'; }
cleanup() {
  local rc=$?
  [ "$rc" = 0 ] || c logs 2>&1 | tail -n 40 >&2 || true
  c down -t 15 >/dev/null 2>&1 || true
  podman rm -f -t 0 "$holder" >/dev/null 2>&1 || true
  while ns umount -l "$VIEW/music" 2>/dev/null; do :; done   # a left-over fuse mount
  while ns umount "$VIEW" 2>/dev/null; do :; done           # our bind
  fusermount3 -u "$LINKS_ROOT/cloud" 2>/dev/null || true
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP" 2>/dev/null || ns rm -rf "$TMP"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
# within SECONDS CMD...: CMD succeeds before a wall-clock deadline (microseconds, bash 5)
within() {
  local end=$((${EPOCHREALTIME/./} + $1 * 1000000)); shift
  until "$@"; do [ "${EPOCHREALTIME/./}" -lt "$end" ] || return 1; sleep 0.1; done
}

"$repo/tests/make_fixtures.sh" "$MUSIC"
# the cloud case: absolute symlinks into a FUSE mount in a subpath of $LINKS_ROOT, which the
# test compose file binds at the identical path; one more points outside it (dangling in there)
"$repo/tests/make_fixtures.sh" "$TMP/store"
mkdir -p "$LINKS_ROOT/cloud"
fuse-overlayfs -o lowerdir="$TMP/store" "$LINKS_ROOT/cloud"
chmod u+w "$MUSIC"
for f in "$LINKS_ROOT/cloud"/*; do ln -s "$f" "$MUSIC/link-${f##*/}"; done
ln -s "$TMP/store/tagged.mp3" "$MUSIC/link-gone.mp3"
chmod 0555 "$MUSIC"
mkdir -p "$CONFIG" "$VIEW"
cat >"$TMP/test.yaml" <<EOF
services:
  beetsfs:
    restart: "no"                                 # a dead daemon must stop the container
    volumes: ["$LINKS_ROOT:$LINKS_ROOT:ro,rslave"] # the cloud case, see compose.yaml
EOF
# SELinux: label the view dir and the links root themselves, as compose.yaml says
[ "$(getenforce 2>/dev/null || echo Disabled)" = Disabled ] || chcon -t container_file_t "$VIEW" "$LINKS_ROOT"
# the view must be a shared mount (rootless: in podman's user namespace) for others to see it
ns sh -c 'mount --bind "$0" "$0" && mount --make-shared "$0"' "$VIEW"

c up -d --build >"$TMP/build.log" 2>&1 || { tail -n 30 "$TMP/build.log" >&2; fail "compose up"; }
within 60 py 'import os; os.path.ismount("/beetsfs/music") or exit(1)' || fail "view not mounted"
[ "$(getenforce 2>/dev/null || echo Disabled)" = Disabled ] \
  || [[ $(podman inspect -f '{{.ProcessLabel}}' "$(podman ps -q --filter "label=io.podman.compose.project=$proj")") == *:container_t:* ]] \
  || fail "beetsfs does not run confined (container_t)"

# 1. import as-is from the CLI; the files show up when the command exits
box beet import -A -q /music >"$TMP/beet.log" 2>&1 || { cat "$TMP/beet.log" >&2; fail "beet import"; }
n=$(box beet ls | wc -l)
[ "$n" -gt 0 ] || fail "beets imported nothing"
nview() { nfiles $V; }
counts_match() { [ "$(nview)" = "$n" ]; }
within 5 counts_match || fail "view has $(nview) files, beets has $n"
# the shipped paths: lay the view out Artist/Album/file (the fixtures carry no artist or album)
box beet modify -a -y albumartist=Artist album=Album >>"$TMP/beet.log" 2>&1 || fail "beet modify album"
depth3() { py 'import os, sys
p = [os.path.relpath(os.path.join(d, f), sys.argv[2]) for d, _, fs in os.walk(sys.argv[2]) for f in fs]
sys.exit(len(p) != int(sys.argv[1]) or any(x.count("/") != 2 for x in p))' "$n" $V; }
within 5 depth3 || fail "view not laid out Artist/Album/file"

# 2. a tag edit shows up in the view within 5 s (indexed when `beet modify` exits):
# the shipped paths end in $title
box beet modify -y title:wrong title=Right >>"$TMP/beet.log" 2>&1 || fail "beet modify"
nright=$(box beet ls title:Right | wc -l)
[ "$nright" -gt 0 ] || fail "no item titled Right"
renamed() { [ "$(py 'import os; print(sum(" Right" in f for _, _, fs in os.walk("/beetsfs/music") for f in fs))')" = "$nright" ]; }
within 5 renamed || fail "title change not visible in the view within 5 s"

# symlinked items are served from their FUSE targets: passthrough byte-identical, retagged
# with the beets title; the dangling link was left out, nothing crashed
vp() { py 'import sqlite3, sys
db = sqlite3.connect("file:/config/beets/beetsfs-index.db?mode=ro", uri=True)
print(db.execute("SELECT vpath FROM files WHERE src_path = ?", (sys.argv[1].encode(),)).fetchone()[0].decode())' "/music/$1"; }
[ "$(md5 "$V/$(vp link-plain.ogg)")" = "$(md5 "$LINKS_ROOT/cloud/plain.ogg")" ] || fail "symlinked passthrough differs"
t=$(py 'import sys; from mediafile import MediaFile; print(MediaFile(sys.argv[1]).title)' "$V/$(vp link-tagged.mp3)")
[ "$t" = Right ] || fail "symlinked mp3 title through the view: $t"
[ -z "$(box beet ls path:/music/link-gone.mp3)" ] || fail "dangling symlink imported"

# beet edit (the tool for correcting data) writes its temp file (tmpfs /tmp on the read-only
# root); an editor that only prints its parent's (beet's) capabilities changes nothing
out=$(c exec -T -e EDITOR="python -c 'import os; print(open(\"/proc/%d/status\" % os.getppid()).read())'" \
  beetsfs beet edit title:Right </dev/null 2>&1) || true
[[ $out == *'No changes'* ]] || fail "beet edit with a no-op editor: $out"
[[ $out == *$'CapEff:\t0000000000000000'* && $out == *$'CapPrm:\t0000000000000000'* ]] \
  || fail "beet runs with capabilities: $(grep ^Cap <<<"$out")"

# 3. read-only source and view
rofs /music/x || fail "/music is not read-only (EROFS)"
rofs $V/x || fail "the view is not read-only (EROFS)"
# read-only root filesystem
rofs /x || fail "/ is not read-only (EROFS)"
rofs /usr/x || fail "/usr is not read-only (EROFS)"
# tag stubs are written to RAM: /tmp is compose's capped tmpfs (podman's default one has no size=256m)
tmpfs=$(py 'l = [l.split(" - ")[1].split() for l in open("/proc/self/mountinfo") if l.split()[4] == "/tmp"][-1]
print(l[0], "size=262144k" in l[2].split(","))')
[ "$tmpfs" = "tmpfs True" ] || fail "/tmp is not the 256 MB tmpfs: $tmpfs"
# PID 1 has exactly the caps compose grants: SYS_ADMIN (bit 21)
capeff=$(py 'print(next(l.split()[1] for l in open("/proc/1/status") if l.startswith("CapEff:")))')
[ "$capeff" = 0000000000200000 ] || fail "CapEff of PID 1 is $capeff, expected only CAP_SYS_ADMIN"
# the daemon drops it once mounted (it reads the index, which beet writes)
dcaps=$(py 'import os
for p in filter(str.isdigit, os.listdir("/proc")):
    if open(f"/proc/{p}/comm").read() == "beetsfs\n":
        print(next(l.split()[1] for l in open(f"/proc/{p}/status") if l.startswith(("CapEff", "CapPrm"))))')
[ "$dcaps" = 0000000000000000 ] || fail "the daemon kept capabilities: $dcaps"
# PID 1's own `beet beetsfs` goes through the capability-dropping wrapper too
w=$(py 'import shutil
env = dict(v.split("=", 1) for v in open("/proc/1/environ").read().split("\0") if "=" in v)
print(shutil.which("beet", path=env["PATH"]))')
[ "$w" = /usr/local/bin/beet ] || fail "PID 1 runs beet as $w, not the wrapper"
# everything in /config belongs to container root (rootless: the host user; rootful: root)
others=$(py 'import os
for root, dirs, files in os.walk("/config"):
    for p in [root] + [os.path.join(root, n) for n in dirs + files]:
        st = os.lstat(p)
        if (st.st_uid, st.st_gid) != (0, 0): print(f"{st.st_uid}:{st.st_gid} {p}")')
[ -z "$others" ] || fail "files in /config not owned by 0:0: $others"

# 4. a second container sees the view, byte-identical to what beetsfs serves
[ "$(consumer find "$CV" -type f | wc -l)" = "$n" ] || fail "consumer sees $(consumer find "$CV" -type f | wc -l) files, expected $n"
v=$(py 'import os, sys; print(next(os.path.relpath(os.path.join(d, f), sys.argv[1]) for d, _, fs in os.walk(sys.argv[1]) for f in fs))' $V)
[ "$(consumer md5sum "$CV/$v" | cut -d' ' -f1)" = "$(md5 "$V/$v")" ] \
  || fail "consumer reads different bytes: $v"
# rootful: the host sees the view too (rootless cannot propagate mounts to the host)
[ -z "$root" ] || [ "$(find "$VIEW/music" -type f | wc -l)" = "$n" ] || fail "host does not see the view"

# 5. a dead daemon stops the container, and its SIGTERM unmount reaches the consumer
killd 15
stopped() { [ "$(state)" = exited ]; }
within 20 stopped || fail "container still $(state) after the daemon exited"
[ "$(consumer find "$CV" -type f | wc -l)" = 0 ] || fail "consumer still sees files after the daemon exited"

# 6. a consumer started while the view is up holds a view file open; `podman compose stop`
# (SIGTERM to PID 1) exits 143 and unmounts even so; after `start` that consumer sees the view again
c start beetsfs >/dev/null 2>&1 || fail "compose start"
within 30 counts_match || fail "view not back after restart"
podman run -d --rm --name "$holder" -v "$VIEW:/view:ro,rslave" \
  docker.io/library/alpine:3.20 sh -c 'exec 3<"$1"; exec sleep 600' sh "$CV/$v" >/dev/null || fail "holder"
holding() { podman exec "$holder" ls -l /proc/1/fd/3 2>/dev/null | grep -q "$CV/"; }
within 10 holding || fail "holder has no view file open"
holder_sees() { [ "$(podman exec "$holder" find "$CV" -type f | wc -l)" = "$n" ]; }
c stop -t 15 beetsfs >/dev/null 2>&1 || fail "compose stop"
[ "$(exitcode)" = 143 ] || fail "exit code $(exitcode) after stop, expected 143"
[ "$(consumer find "$CV" -type f | wc -l)" = 0 ] || fail "consumer still sees files after stop"
c start beetsfs >/dev/null 2>&1 || fail "compose start after stop"
within 30 counts_match || fail "view not back after stop and start"
within 5 holder_sees || fail "holder does not see the view after stop and start"

# 7. SIGKILLed daemon: PID 1 unmounts at once (the view is empty, not dead), the container stops
killd 9
within 20 stopped || fail "container still $(state) after SIGKILL of the daemon"
[ "$(consumer find "$CV" -type f | wc -l)" = 0 ] || fail "consumer still sees files after SIGKILL of the daemon"
c start beetsfs >/dev/null 2>&1 || fail "compose start after SIGKILL"
within 30 counts_match || fail "view not back after SIGKILL and start"
within 5 holder_sees || fail "holder does not see the view after SIGKILL and start"
# the whole container SIGKILLed: the dead mount stays until `podman compose start` (or `down` +
# `up`) clears it (the entrypoint's lazy unmount propagates out) and every consumer sees the
# view again, with nothing to do host-side
podman kill -s KILL "$(podman ps -q --filter "label=io.podman.compose.project=$proj")" >/dev/null
within 20 stopped || fail "container still $(state) after SIGKILL of the container"
c start beetsfs >/dev/null 2>&1 || fail "compose start after container SIGKILL"
within 30 counts_match || fail "view not back after container SIGKILL and start"
c logs beetsfs 2>&1 | grep -q "cleared a stale mount" || fail "no stale-mount cleanup logged"
[ "$(consumer find "$CV" -type f | wc -l)" = "$n" ] || fail "new consumer does not see the view after SIGKILL and start"
within 5 holder_sees || fail "holder does not see the view after container SIGKILL and start"
podman rm -f -t 0 "$holder" >/dev/null
# the same after `down` and a fresh `up`
killd 9
within 20 stopped || fail "container still $(state) after the second SIGKILL"
c down -t 15 >/dev/null 2>&1 || fail "compose down after SIGKILL"
c up -d >/dev/null 2>&1 || fail "compose up after SIGKILL and down"
within 30 counts_match || fail "view not back after SIGKILL, down and up"
[ "$(consumer find "$CV" -type f | wc -l)" = "$n" ] || fail "consumer does not see the view after SIGKILL, down and up"
c down -t 15 >/dev/null 2>&1 || fail "compose down"
[ -z "$(state)" ] || fail "container left after down"
[ "$(consumer find "$CV" -type f | wc -l)" = 0 ] || fail "consumer still sees files after down"
[ -z "$root" ] || [ "$(find "$VIEW/music" -type f | wc -l)" = 0 ] || fail "host still sees files after down"
t=$(ns stat -f -c %T "$VIEW/music") && [ "$t" != fuse ] || fail "view still a fuse mount after down"
echo "OK ($n files)"
