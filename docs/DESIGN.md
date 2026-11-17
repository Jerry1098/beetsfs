# beetsfs — design

## Intent

A music collection lives in a directory that must never be modified (read-only
mount, NAS snapshot, seeding torrents, ...). Its tags and file names are messy.

beetsfs gives a second, read-only view of that collection where every file has
the tags and the path beets thinks are correct. The audio bytes are served
straight from the original files; nothing is copied and nothing in the source
is written. Players and media servers (Navidrome, Jellyfin, mpd, ...) point at
the view instead of the source.

Everything runs in one container: beets, driven from its CLI
(`podman compose exec beetsfs beet ...`), and the FUSE mount.

## Goals

- Source directory is mounted `:ro` and is never written. The mount, not configuration,
  stops beets and accidents (`beet write`, `beet mv`, `beet rm -d` get EROFS). It is not a
  hard guarantee: PID 1 holds SYS_ADMIN and could remount `/music` read-write. Rootless, container root is your user: for a hard guarantee the music must not
  be writable by your user on the host (e.g. `sudo chown -R root:root <music>` once) or be
  a read-only mount made by host root. Rootful, see Security.
- Files in the view carry the tags from the beets library and sit at the path
  given by the beets `paths:` formats.
- Audio data is byte-identical to the source and read from it at request time.
- Reads are fast enough to be invisible: the hot path is Rust doing `pread`.
- A tag edit (`beet modify`, `beet edit`) shows up in the mount within seconds,
  without a restart.
- One image, one `compose.yaml`.

## Non-goals (v1)

- Writing through the mount. It is read-only (`EROFS`).
- Showing files beets has not imported. Only library items appear.
- Formats other than MP3 and FLAC get *retagged*. Everything else is still
  served, with its original tags, at the beets path (see Formats).
- Album art fetched by beets (`fetchart` wants to write into the album
  directory, which is read-only). Art already embedded in the source is kept.
- Transcoding, ReplayGain analysis writes, multi-library, auth.

## Anti-goals

- Corrupt or shifted audio. If anything about a file is uncertain, serve the
  original bytes unchanged rather than a guess.
- A second implementation of beets' tag mapping that slowly drifts from beets.

## Approaches

**A. beets plugin precomputes the tag bytes, Rust only splices (chosen).**
A beets plugin uses beets' own tag writer (MediaFile/mutagen) to produce the
new tag block for each item and stores it in a small index database. The Rust
daemon knows nothing about audio formats: a virtual file is
a list of segments, each either stored bytes or a byte range of the source
file.
Tags are exactly what `beet write` would have produced. Rust stays tiny.
Cost: the index holds one tag block per track (see Known ceilings).

**B. Rust reads `library.db` and writes the tags itself (e.g. `lofty`).**
No Python on the write side, no index. But the mapping "beets field → ID3
frame / Vorbis comment / MP4 atom" is several thousand lines in MediaFile and
would have to be re-created and kept in sync, per format. Rejected: most code,
and it violates the second anti-goal.

**C. FUSE in Python, tags generated on the fly.**
Least code, but Python sits in the read path of every audio byte. Rejected:
the speed requirement is the reason Rust is here.

(Not FUSE at all — reflink/copy plus `beet write` — needs writable storage the
size of the tag-changed data and a second tree to keep in sync. Out by
requirement.)

## Architecture

```
            ┌──────────────────────── container ────────────────────────┐
            │                                                           │
 beet CLI ─▶│ ───────────────────────────────▶ beets ──▶ library.db     │
            │                                   │                       │
            │                         beetsfs plugin (Python)           │
            │                    reads source header, writes tag block  │
            │                                   ▼                       │
            │                              index.db (SQLite, WAL)       │
            │                                   ▲ queried per FUSE call │
            │                         beetsfs daemon (Rust, fuser)      │
            │                            │                  │           │
            └────────────────────────────┼──────────────────┼───────────┘
                                   pread │                  │ FUSE
                              /music (ro bind)  /beetsfs/music (in the rshared volume)
                                                            │
                                                  players / media servers
```

Three parts, two processes of ours:

| Part | Language | Job |
|---|---|---|
| beets | stock, CLI | Import, autotag, edit. Unchanged. |
| `beetsplug/beetsfs.py` | Python | Keep `index.db` in step with the beets library. |
| `beetsfs` | Rust | Serve `index.db` as a read-only FUSE filesystem. |

The only interface between Python and Rust is `index.db`. No sockets, no RPC.

### beets configuration (the part that makes it non-destructive)

[`docker/config/beets/config.yaml`](../docker/config/beets/config.yaml), copied to
`/config/beets` on first start: `directory: /beetsfs/music` (only used to compute
relative paths), `import: {copy: no, move: no, write: no}`, `paths:` laying the view out
Artist/Album/Songs, and the `beetsfs` plugin.

With `copy/move/write: no`, beets records the original path of each file and
keeps corrected metadata only in its database. That is a stock beets mode.
The `:ro` mount is the backstop if some plugin tries to write anyway.

### Index (`index.db`)

`beetsfs-index.db` next to beets' `library` (in the container
`/config/beets/beetsfs-index.db`, the path the entrypoint gives the daemon: it assumes
beets' `library:` stays `/config/beets/library.db`). `PRAGMA user_version = 2`, WAL mode. The plugin's `SCHEMA` is the source of truth (the
Rust tests load it from there), [`plugin/beetsplug/beetsfs.py`](../plugin/beetsplug/beetsfs.py).

- `dirs (id, parent, name)`: the directory tree of the view, served as is (no tree in
  memory). `id` is the directory's inode, never reused; root 1 with parent 0.
- `files`: one row per beets item: `vpath`, `dir_id` and `name`, the source path with its
  size and mtime at indexing time, the virtual `size`, `meta_hash` (the item's tag fields)
  and the reported `mtime` (moves forward on every change, also two within one second).
- `segments (item_id, seq, blob_hash, offset, len)`: a virtual file is its segments in
  `seq` order, each a range of the source file (`blob_hash` NULL) or of a stored blob.
- `blobs (hash, data)`: content-addressed (SHA-256), so identical bytes are stored once
  for the whole library.

A plain passthrough file is one segment: the whole source. A retagged file
with an unchanged cover is typically five:

```
blob   new tag bytes up to the cover      < 1 kB
source the cover, where it already is     (any size, costs nothing)
blob   new tag bytes after the cover      a few hundred bytes
blob   mutagen's zero padding             range of one shared 64 KiB zero blob
source the audio
```

Measured on the example albums (10 tracks): 1.6 MB of header per FLAC track and 440 kB per
MP3 track shrink to ~1 kB stored per track.

`vpath` comes from `item.destination()` relative to `directory`, so the layout
is controlled by the normal beets `paths:` section and nothing new needs
configuring. On a collision the plugin appends ` [item_id]` before the
extension. The plugin writes the `dirs` chain for each file's directory and deletes
directories left empty (walking up from the old directory when a file moves or goes). It is the only gatekeeper: a vpath
with an empty, `.` or `..` component, a leading `/` or a NUL byte is logged and left out of
the view. A file and a directory of the same name in one directory (rare: beets paths carry
extensions): the directory wins, the daemon hides the file.

### Plugin: producing the segments

One function, `reconcile(lib, conn, ids)`, does all the work. For each item:

1. Compute `vpath` and `meta_hash`. If the row exists with the same hash and
   the source `size`/`mtime` are unchanged, skip.
2. Find the audio region `[audio_start, audio_end)` of the source:
   - MP3: skip a leading ID3v2 tag (size is in its 10-byte header); drop a
     trailing 128-byte ID3v1 tag if present.
   - FLAC: walk metadata block headers until the last-block flag.
3. **Stub trick.** Copy `source[0 : audio_start + K]` (K = 6 KiB: mutagen needs
   one full MP3 frame, measured worst case 1444 bytes, to accept the file; 4x margin) to a temp file and call
   `item.write(path=stub)`. beets writes its tags into the stub exactly as it
   would into the real file.
4. Check that the last K bytes of the stub equal
   `source[audio_start : audio_start + K]`. Then `head = stub[:-K]` is the new
   metadata and the audio segment is `source[audio_start : audio_end)`.
5. **Covers.** For each picture in the new `head` (read with mutagen), search
   for its raw image bytes in the source's old header.
   - Found → the cover is unchanged: cut it out of `head` and emit a source
     segment pointing at where it already is. This is a byte comparison, so
     there are no false positives.
   - Not found → beets changed or added the cover (or the source stores it in
     an encoding that differs byte-wise). Its bytes stay in a blob. Blobs are
     keyed by SHA-256, so an album's cover is stored once, not once per track.
6. What is left of `head` between the cut-outs becomes blob segments. A trailing zero run
   (64 bytes or more, mutagen's padding) points into one shared blob of 64 KiB zeros
   instead of being stored.
7. If any step fails, the check does not hold, or more than 1 MiB of tag bytes would be
   stored → write a passthrough row and log a warning. Never guess. A *read* error on the
   source (network) is different: the item is skipped, not stored, and retried next time;
   so is a source that cannot be `stat`'ed (deleted, or a cloud mount that is down): its row
   stays. Rows are deleted only when the item leaves the library.

The source is opened once, unbuffered (a buffered file reads a whole 128 KiB buffer per
seek). Bytes read from the source per retagged file: a few header probes plus
`audio_start + 6 KiB`, which includes any embedded cover (measured: 1.6 MB per FLAC
track of the example albums, 440 kB per MP3).

Rows whose item no longer exists are deleted, and blobs no longer referenced
by any segment are deleted with them, in one short transaction at the end of a full
reconcile. The unchanged check and building the segments take no lock; only writing a
changed item does (one transaction per item, `synchronous=NORMAL`), so another `beet`
process waiting on the write lock gets it at once even during a long scan.

Every write is decided on a fresh `lib.get_item()`: a full scan's `lib.items()` snapshot
only decides "unchanged". Under the lock the item is read once more and the write skipped if
its tags, source path or destination changed meanwhile: the reconcile that sees that change
writes it. When only the destination changed (a `paths:` edit, a rename, `beet move`), the
row moves and keeps its segments: no source read.

Triggers:

- beets event `database_change` → the item's id (or the album's item ids) is collected.
  The event fires inside beets' open transaction, before the commit, and beets has no
  event after it, so the ids are indexed when the `beet` process exits (`atexit`, so also
  after an error or Ctrl-C; the plugin turns SIGTERM and SIGHUP, e.g. an ssh drop, into a
  normal exit; not after SIGKILL): outside beets' locks, and from the committed library. During
  an import the view therefore follows when the import ends.
- `beet beetsfs` → full reconcile, run once at container start: it catches what a killed
  `beet` missed, changed sources, and a changed `paths:` config. An unchanged library costs
  one `stat` per source file plus beets building every item (measured at 50k items: 19 s,
  440 MB RSS).

### Daemon: `beetsfs <index.db> <mountpoint>`

Crates: `fuser`, `rusqlite` (bundled SQLite), `libc`.

- **No tree in memory, no reload.** One read-only connection to `index.db`, opened lazily;
  every lookup/getattr/readdir/open is an indexed query (WAL readers never block on the
  plugin). An edit is visible on the next call after the kernel's 1 s attribute/entry TTL.
  If the index is missing, unreadable or not `user_version` 2, the root is an empty
  directory; the error is logged once per change, and the connection reopened on the next call.
  Mount read-only with `allow_other`, `default_permissions`.
- **Inodes:** files `item_id + 2^32` (stable across restarts, so NFS-style
  consumers and caches behave), directories `dirs.id` (root 1).
- **lookup:** `dirs` by `(parent, name)`, then `files` by `(dir_id, name)`.
  **getattr:** the `dirs` row by id or the `files` row by item_id.
- **readdir:** `.`, `..`, child directories by id, then files by item_id (files hidden by a
  directory of the same name skipped). The offset of an entry is its inode + 1 (`.` 1,
  `..` 2): directory inodes are below file inodes, so a page queries only the children with
  inode ≥ offset and stops when the reply is full; each row is read once.
- **open:** load the file row, its segments and blobs in one snapshot, open the source
  (`O_NONBLOCK`, symlinks followed), `fstat` it; anything but a regular file is EIO. If
  size or mtime differ from the row, this handle serves the *whole original file* instead
  (and logs); offsets into a changed file would be garbage. The file handle is the source's
  fd. No `FOPEN_KEEP_CACHE`, so each open drops the page cache (cloud mounts cache below
  us). Open handles keep what they were opened with.
- **Threads:** 4 FUSE worker threads, so a slow source read does not stall lookups,
  listings or other streams; `read` does not hold the handle table's lock during `pread`.
  The index connection has one lock (queries take microseconds).
- **read:** walk the segments covering the requested range; copy from the
  blob or `pread` from the source. Reads may straddle segment boundaries.
- **Capabilities:** once mounted the daemon drops all of them (it parses the index, which
  `beet` writes), so as root it cannot unmount. `panic = "abort"`: with 4 threads a panic
  ends the process, and the container restarts it.
- **Shutdown:** on SIGTERM, as root (the container) the daemon just exits; PID 1 then
  lazily unmounts (`umount2(MNT_DETACH)`), also after a crash or SIGKILL of the daemon, so
  consumers see an empty `music`, not a dead mount. Lazy, so a consumer holding a file or its
  cwd in the view does not block it (a plain umount fails with EBUSY). As a normal user
  (`tests/mount_test.sh`) fuser runs `fusermount3 -u -z`. No `auto_unmount`: with it fuser
  leaves the unmount to the `fusermount3` helper, which in a rootless podman container exited
  without unmounting; the image has no `fusermount3`.

`st_mtime` of a file is the row's `mtime`, so media servers notice retags on
their normal rescan.

### Container

Target engine: **podman**, rootless with `podman compose` (podman-compose), or rootful (the
same compose file, or the quadlet [`deploy/beetsfs.container`](../deploy/beetsfs.container)).
[`compose.yaml`](../compose.yaml) carries the setup steps in its comments and a
commented-out consumer service.

- **Image:** three stages, Debian 13 throughout, each pinned by digest: `rust:1-slim-trixie`
  builds `beetsfs`; `debian:13-slim` (the same Python 3.13 as distroless) builds `/venv`
  with `uv sync --frozen` from `plugin/uv.lock` and gathers `nano` and the xterm terminfo;
  the final `gcr.io/distroless/python3-debian13` has no shell and no package manager.
  `BEETSDIR=/config/beets`, `EDITOR=nano`. Everything runs as container root: the daemon
  must mount (rootless, container root is the host user, so `CONFIG` files are yours).
- **Hardening:** `cap_drop: [ALL]` plus `SYS_ADMIN` (mount/umount for FUSE; e2e checks PID
  1's `CapEff` is exactly that), `no-new-privileges`, `read_only: true`; writable are
  `/config`, the view, and `/tmp`, a 256 MB RAM tmpfs for the tag stubs and `beet edit`.
  Only PID 1 keeps SYS_ADMIN: the daemon drops it once mounted, and `beet` on `PATH` is
  [`docker/beet.py`](../docker/beet.py), which drops every capability and execs the real beet,
  so the code that parses untrusted tags runs without any (e2e checks both, and that PID 1's
  `beet beetsfs` goes through the wrapper). `no-new-privileges` keeps them from coming back;
  the wrapper refuses to run without it (a hand-written quadlet may forget it).
- **SELinux** stays on (`container_t`, e2e checks it). `/config` is `:Z`, the music `:z` (for
  a dir of symlinks: the links only). The view dir and a links root get
  `chcon -t container_file_t` once, on the directory itself: `:z` on the view fails after a
  SIGKILL (its relabel walk hits the dead mount), and FUSE cannot be relabelled. Confined
  containers can mount FUSE and read FUSE files (`fusefs_t`) with every `*fusefs*` boolean
  off (Fedora 44, container-selinux 2.251); no `setsebool`.
- **Entrypoint** ([`docker/entrypoint.py`](../docker/entrypoint.py), stdlib, PID 1): clear a
  stale mount → copy the default beets config on first start → start `beetsfs` → run
  `beet beetsfs` once in a thread → when the daemon exits, unmount the view. It forwards
  SIGTERM/SIGINT to the daemon (exit 143) and exits with the daemon's status, so a dead
  daemon stops the container and the restart policy brings it back.
- **beets config** also enables `musicbrainz` (autotagging), `chroma` and `edit`. `chroma`
  matches by acoustic fingerprint (AcoustID): at import it decodes the first 120 s of every
  track with `fpcalc`, built in its own stage from Chromaprint and a minimal FFmpeg (audio
  decoders only), linked statically; both sources pinned by sha256.
  That is the one place that reads audio from the sources, several MB per track; drop
  `chroma` from `plugins:` to import without it.

```sh
podman compose exec beetsfs beet import /music              # import in place, nothing copied
podman compose exec beetsfs beet edit <query>               # correct wrong data in $EDITOR
podman compose exec beetsfs beet modify <query> field=value
```

**The view and its consumers.** The volume is `$VIEW:/beetsfs:rshared` and the daemon
mounts one level down, at `/beetsfs/music`. `$VIEW` must be a shared mount, once per boot
before the start (rootless: `podman unshare sh -c 'mount --bind "$0" "$0" && mount
--make-shared "$0"' "$VIEW"`; rootful: the same as root, the quadlet's `ExecStartPre=`).
Without it nothing outside the container sees the view, and the container cannot tell (its
mountinfo looks the same either way). A consumer binds `$VIEW`, a plain directory,
`ro,rslave` (library at `<mount>/music`), default user namespace, confined: it sees the
view whenever beetsfs is up, also across stop/start, and an empty `music` while it is down.
Rootful, the host and every rootful container see it; rootless, the user's other rootless
containers do, the host does not.

- **User namespace** (rootless): the default mapping, not `keep-id`: the FUSE mount belongs
  to podman's user namespace, which all of the user's default containers share. With
  `keep-id` the kernel refuses the view to everyone outside the container. Rootful: no user
  namespace, and not `UserNS=auto` either (a FUSE mount made in a child user namespace is
  "Permission denied" to the parent namespace, `allow_other` or not).
- **After a SIGKILL** of the whole container (of the daemon alone, PID 1 unmounts at once):
  a dead mount stays at `$VIEW/music`. On the next start the entrypoint lazily unmounts it
  (only a fuse mount with source `beetsfs`); the volume is `rshared`, so that clears it
  outside too, and every consumer sees the new mount. Nothing to do host-side.

**Symlinked sources** (e.g. a music dir of absolute symlinks into another container's cloud
FUSE mount at `/root/mount/<sub>`): bind the PARENT of that FUSE mount at the identical path,
`ro,rslave`, so an upstream restart propagates. The daemon follows symlinks; a link whose
target is missing is not imported. Cloud mounts must give container root read access
(`allow_other`; files root may read without CAP_DAC_OVERRIDE, e.g. not mode 0600 for another
uid). Network cost: chroma's fingerprinting at import (above), the plugin's bytes per file (once per file, and again per tag change),
one `stat` per source at container start, one `stat` per open, and the read ranges a player
asks for. Where a cloud mount reports unstable mtimes, beetsfs serves the original tags (see
`open`) until the next reconcile.

**Security.** Rootless, SYS_ADMIN is held in the user namespace only. Rootful, container root
is host root and SYS_ADMIN is real. What limits it: SELinux confinement, every other
capability dropped, `no-new-privileges`, a read-only root, `beet` (the tag parser) without
capabilities, the daemon without capabilities once mounted. What does not: code running as
PID 1 could unmount, remount or
mount things in the container's mount namespace, `/music` and the bound cloud mount
read-write among them (`:ro` is a mount flag), and a mount it makes under the rshared view
propagates to the host. The index is a trust boundary: whoever can write
`/config/beets/beetsfs-index.db` chooses which file the daemon opens (`src_path`) and serves
to every consumer.

## Formats

| Format | v1 | Notes |
|---|---|---|
| MP3 | retagged | ID3v2 in front, audio untouched. Trailing APEv2 tags are left in place. |
| FLAC | retagged | Metadata blocks in front; STREAMINFO and pictures carried over by mutagen. |
| M4A/ALAC | passthrough | Doable with the same segment model, including files with `moov` at the end; chunk offsets are rewritten by mutagen. Next after v1. |
| Ogg/Opus | passthrough | Hard: a comment header that changes page count renumbers every later page. Needs padding tricks or stays passthrough. |
| others | passthrough | |

Passthrough files still get the correct *path*; only their embedded tags are
the originals.

## Known ceilings

- **Index size.** About 1 kB of tag bytes per track (padding is not stored) plus rows and
  indexes. Covers only count when beets replaced them, once per distinct image. At 50k
  tracks: 89 MB measured.
- **Page cache across a retag.** A handle opened before a retag can fill the kernel's page
  cache with the old bytes while a new handle reads, until both close or the 1 s TTL passes
  (old tags or a cut-off tail on that one open). Fix if it matters: one inode per content
  version.
- **No kernel passthrough.** Linux FUSE passthrough maps whole backing files,
  not offset ranges, so it does not fit spliced files.
- **Source changes** are noticed by the next reconcile of that item (container start, or a
  `beet` command touching it); until then the file is served as the plain original (see
  `open`). Not `beet update`: it would overwrite beets' corrected tags with the source's.

## Testing

- **Plugin (pytest, `uv run --project plugin pytest plugin/tests`):** for generated MP3 and
  FLAC fixtures (`tests/make_fixtures.sh`) and the albums in `tests/music` if present,
  assemble the segments in memory and compare the tags with a real `beet write`; audio and
  covers come from the source; reconcile, events, paths and the write lock.
- **Daemon (`cargo test` in `fuse/`):** the splice function, with reads crossing each
  boundary and past EOF; the index queries against a database built from the plugin's schema;
  a FIFO source is an error, not a hang.
- **Content (`tests/mount_test.sh`, no container):** the release binary mounts an index built
  by real beets from the fixtures or `MUSIC=dir`; decoded audio identical to the source,
  tags from beets, passthrough byte-identical, an absolute-symlink source, edits visible, read-only, a vanished or
  changed source, SIGTERM with a file open.
- **Container plumbing (`tests/e2e.sh` rootless, `sudo tests/e2e.sh` rootful):** confined
  (`container_t`), symlinked sources into a FUSE mount, view laid out Artist/Album/file,
  a consumer container reads the same bytes, a tag edit visible within 5 s; hardening
  (caps exactly SYS_ADMIN, `beet` without any, read-only root, `/tmp` tmpfs, EROFS on
  source and view);
  signals and exit codes, a busy `stop`, SIGKILL recovery, `down` leaves no mount behind.
