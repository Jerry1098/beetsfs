import atexit
import hashlib
import logging
import os
import signal
import sqlite3
import sys
import tempfile
import time

import mutagen
from beets import config, ui
from beets.library import Item
from beets.plugins import BeetsPlugin

K = 6 * 1024  # audio bytes in the stub: 4x the measured minimum (1444 B: one max-size MP3 frame + next header)
ZEROS = b"\0" * 65536  # one shared blob for the zero padding mutagen puts at the end of a tag
MAX_HEAD = 1 << 20  # stored tag bytes per item above this: passthrough instead
log = logging.getLogger("beets.beetsfs")

# (data, offset, length); data None = range of the source file
Seg = tuple[bytes | None, int, int]


def audio_region(f, name: bytes, size: int) -> tuple[int, int] | None:
    """(audio_start, audio_end) of a retaggable MP3/FLAC open as f, else None."""
    head = f.read(4)
    if head == b"fLaC":
        pos = 4
        while True:
            f.seek(pos)
            h = f.read(4)
            if len(h) < 4:
                return None
            pos += 4 + int.from_bytes(h[1:], "big")
            if h[0] & 0x80:
                break
        start, end, ok = pos, size, (0xFF, 0xF8, 0xFE)
    elif name.lower().endswith(b".mp3"):
        start = 0
        if head[:3] == b"ID3":
            h = head + f.read(6)
            if len(h) < 10:
                return None
            s = h[6:10]
            start = 10 + (s[0] << 21 | s[1] << 14 | s[2] << 7 | s[3])
            if h[5] & 0x10:
                start += 10
        end = size
        if size - 128 > start:
            f.seek(size - 128)
            if f.read(3) == b"TAG":
                end = size - 128
        ok = (0xFF, 0xE0, 0xE0)
    else:
        return None
    if start >= end:
        return None
    f.seek(start)
    b = f.read(2)
    if len(b) < 2 or b[0] != ok[0] or b[1] & ok[2] != ok[1]:
        return None
    return start, end


def read_exact(f, n: int) -> bytes:
    """n bytes, fewer only at EOF; one unbuffered read may return less (network filesystems)."""
    b = b""
    while len(b) < n and (c := f.read(n - len(b))):
        b += c
    return b


def pictures(path: bytes) -> list[bytes]:
    f = mutagen.File(path)
    if hasattr(f, "pictures"):
        return [p.data for p in f.pictures]
    return [a.data for a in f.tags.getall("APIC")] if f.tags else []


def _blob(segs, data):
    """Append data as blob segments; a trailing zero run of 64 bytes or more points into ZEROS."""
    z = len(data) - len(data.rstrip(b"\0"))
    if z < 64:
        z = 0
    if len(data) > z:
        segs.append((data[:len(data) - z], 0, len(data) - z))
    while z:
        n = min(z, len(ZEROS))
        segs.append((ZEROS, 0, n))
        z -= n


def build_segments(item) -> list[Seg]:
    """Segments of the retagged file; the original file if it cannot be retagged. Read errors
    propagate: the caller skips the item and the next reconcile retries it."""
    src = bytes(item.path)
    # unbuffered: a buffered file reads a whole buffer (st_blksize, often 128 KiB) per seek
    with open(src, "rb", buffering=0) as f:
        size = os.fstat(f.fileno()).st_size
        try:
            return _retag(item, f, src, size) or [(None, 0, size)]
        except OSError:
            raise
        except Exception:
            log.warning("beetsfs: falling back to passthrough for %r", src, exc_info=True)
            return [(None, 0, size)]


def _retag(item, f, src, size) -> list[Seg] | None:
    region = audio_region(f, src, size)
    if region is None:
        return None
    a, e = region
    k = min(K, e - a)
    f.seek(0)
    old = read_exact(f, a + k)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(src)[1].decode("latin-1")) as t:
        t.write(old)
        t.flush()
        stub = os.fsencode(t.name)
        item.write(path=stub)
        with open(stub, "rb") as g:
            new = g.read()
        pics = pictures(stub)
    if new[-k:] != old[a:]:
        raise ValueError("audio moved in stub")
    head, old = new[:-k], old[:a]
    cuts = []  # (pos_in_head, pos_in_source, length)
    for pic in pics:
        if len(pic) >= 4096 and (h := head.find(pic)) >= 0 and (o := old.find(pic)) >= 0:
            cuts.append((h, o, len(pic)))
    segs, pos = [], 0
    for h, o, n in sorted(cuts):
        if h < pos:
            continue  # overlapping/duplicate picture: leave it in the blob
        _blob(segs, head[pos:h])
        segs.append((None, o, n))
        pos = h + n
    _blob(segs, head[pos:])
    if sum(n for d, _, n in segs if d is not None and d is not ZEROS) > MAX_HEAD:
        raise ValueError("tag bytes larger than MAX_HEAD")
    segs.append((None, a, e - a))
    return segs


SCHEMA = """
CREATE TABLE IF NOT EXISTS dirs (
  id     INTEGER PRIMARY KEY AUTOINCREMENT, -- FUSE inode of the directory, never reused; root 1
  parent INTEGER NOT NULL,      -- 0 for the root
  name   BLOB NOT NULL,         -- one path component, bytes; '' for the root
  UNIQUE (parent, name)
);
INSERT OR IGNORE INTO dirs VALUES (1, 0, x'');
CREATE TABLE IF NOT EXISTS files (
  item_id      INTEGER PRIMARY KEY,  -- beets item id; inode is derived from it
  vpath        BLOB NOT NULL UNIQUE, -- path inside the mount, relative
  dir_id       INTEGER NOT NULL REFERENCES dirs, -- directory holding the file
  name         BLOB NOT NULL,        -- last path component
  src_path     BLOB NOT NULL,        -- absolute source path (beets paths are bytes)
  src_size     INTEGER NOT NULL,     -- source stat at generation time
  src_mtime_ns INTEGER NOT NULL,
  size         INTEGER NOT NULL,     -- virtual file size (sum of segment lengths)
  meta_hash    BLOB NOT NULL,        -- sha256 of the item's tag fields
  mtime        INTEGER NOT NULL      -- reported st_mtime; moves forward whenever the row changes
);
CREATE INDEX IF NOT EXISTS files_dir ON files (dir_id, name);
CREATE INDEX IF NOT EXISTS dirs_parent ON dirs (parent, id);           -- readdir pages in id order
CREATE INDEX IF NOT EXISTS files_dir_item ON files (dir_id, item_id); -- the same for files
CREATE TABLE IF NOT EXISTS segments (
  item_id   INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
  seq       INTEGER NOT NULL,
  blob_hash BLOB,                    -- NULL -> read from src_path
  offset    INTEGER NOT NULL,        -- into the source file, or into the blob
  len       INTEGER NOT NULL,
  PRIMARY KEY (item_id, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blobs (hash BLOB PRIMARY KEY, data BLOB NOT NULL); -- rowid table: rows are KBs
PRAGMA user_version=2;
"""


def open_index(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT in reconcile
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL: a commit per changed item stays cheap
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def meta_hash(item) -> bytes:
    return hashlib.sha256(repr([(f, item.get(f)) for f in sorted(Item._media_tag_fields)]).encode()).digest()


def _prune(conn, d):
    """Delete directory d and its parents while they are empty; never the root."""
    while d != 1 and not conn.execute(
            "SELECT 1 FROM files WHERE dir_id=?1 UNION ALL SELECT 1 FROM dirs WHERE parent=?1", (d,)).fetchone():
        (parent,) = conn.execute("SELECT parent FROM dirs WHERE id=?", (d,)).fetchone()
        conn.execute("DELETE FROM dirs WHERE id=?", (d,))
        d = parent


def _delete(conn, item_id) -> int:
    old = conn.execute("SELECT dir_id FROM files WHERE item_id=?", (item_id,)).fetchone()
    if old:
        conn.execute("DELETE FROM files WHERE item_id=?", (item_id,))
        _prune(conn, old[0])
    return int(bool(old))


def _write(conn, item, vpath, src_size, src_mtime_ns, segs):
    """segs None: only the path changed, the row keeps its segments (no source read)."""
    *parents, name = vpath.split(b"/")
    d = 1
    for c in parents:
        conn.execute("INSERT OR IGNORE INTO dirs (parent, name) VALUES (?,?)", (d, c))
        (d,) = conn.execute("SELECT id FROM dirs WHERE parent=? AND name=?", (d, c)).fetchone()
    old = conn.execute("SELECT dir_id, mtime FROM files WHERE item_id=?", (item.id,)).fetchone()
    if segs is None:
        conn.execute("UPDATE files SET vpath=?, dir_id=?, name=? WHERE item_id=?", (vpath, d, name, item.id))
        _prune(conn, old[0])
        return
    conn.execute("DELETE FROM files WHERE item_id=?", (item.id,))
    mtime = max(int(time.time()), old[1] + 1 if old else 0)  # two changes within 1 s: still a new mtime
    conn.execute(
        "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
        (item.id, vpath, d, name, bytes(item.path), src_size, src_mtime_ns,
         sum(n for _, _, n in segs), meta_hash(item), mtime),
    )
    for seq, (data, off, n) in enumerate(segs):
        h = None
        if data is not None:
            h = hashlib.sha256(data).digest()
            conn.execute("INSERT OR IGNORE INTO blobs VALUES (?,?)", (h, data))
        conn.execute("INSERT INTO segments VALUES (?,?,?,?,?)", (item.id, seq, h, off, n))
    if old:
        _prune(conn, old[0])  # after the insert, so a directory the file stays in is kept


def _vpath(conn, item) -> bytes | None:
    """The item's path in the view (` [id]` added on a collision); None if it is unusable."""
    vpath = bytes(item.destination(relative_to_libdir=True))
    owner = conn.execute("SELECT item_id FROM files WHERE vpath=?", (vpath,)).fetchone()
    if owner and owner[0] != item.id:
        base, ext = os.path.splitext(vpath)
        vpath = base + b" [%d]" % item.id + ext
    if b"\0" in vpath or any(c in (b"", b".", b"..") for c in vpath.split(b"/")):
        log.warning("beetsfs: leaving %r out of the view: unusable path %r", item.path, vpath)
        return None
    return vpath


def _locked(conn, f, *args):
    """f(conn, *args) in a write transaction: the only time reconcile holds the lock."""
    conn.execute("BEGIN IMMEDIATE")
    with conn:  # commit, or roll back on an error
        return f(conn, *args)


def _row(conn, item_id):
    return conn.execute(
        "SELECT vpath, meta_hash, src_size, src_mtime_ns, src_path FROM files WHERE item_id=?", (item_id,)
    ).fetchone()


def _state(conn, item):
    """The row the item should have, as _row returns it; None: not in the view. A source that
    cannot be stat'ed (gone, or a cloud mount that is down) raises: the item is skipped and
    keeps its row. Rows only go when the item leaves the library."""
    st = os.stat(item.path)
    vpath = _vpath(conn, item)
    return vpath and (vpath, meta_hash(item), st.st_size, st.st_mtime_ns, bytes(item.path))


def _key(item):
    return meta_hash(item), bytes(item.path), bytes(item.destination(relative_to_libdir=True))


def _reconcile_item(conn, lib, item_id, stale=None) -> int:
    row = _row(conn, item_id)
    if stale is not None and row and _state(conn, stale) == row:
        return 0  # a scan's snapshot item only decides "unchanged"; anything else uses a fresh copy
    item = lib.get_item(item_id)
    want = item and _state(conn, item)
    if want == row:
        return 0
    if not want:
        return _locked(conn, _delete, item_id)
    # slow (reads the source, runs beets' writer): outside the lock; skipped when only the path changed
    segs = None if row and want[1:] == row[1:] else build_segments(item)
    key = _key(item)

    def write(conn):
        if (fresh := lib.get_item(item_id)) is None or _key(fresh) != key:
            return 0  # the library moved on meanwhile: the reconcile that saw the change writes it
        vpath = _vpath(conn, item)  # again: another writer may have taken the path meanwhile
        if vpath is None:
            return _delete(conn, item_id)
        _write(conn, item, vpath, want[2], want[3], segs)
        return 1
    return _locked(conn, write)


def reconcile(lib, conn, ids=None) -> int:
    """Bring the rows of item ids (None = whole library) in line; returns rows written or deleted."""
    n = 0
    work = ((i.id, i) for i in lib.items()) if ids is None else ((i, None) for i in ids)
    for item_id, stale in work:
        try:
            n += _reconcile_item(conn, lib, item_id, stale)
        except OSError as e:  # the source (network): no traceback, there may be thousands
            log.warning("beetsfs: skipping item %d: %s", item_id, e)
        except Exception:
            log.warning("beetsfs: skipping item %d", item_id, exc_info=True)
    if ids is None:
        def orphans(conn):
            # the library now, under the lock: others may have added items since the scan
            with lib.transaction() as tx:
                live = {i for (i,) in tx.query("SELECT id FROM items")}
            rows = [i for (i,) in conn.execute("SELECT item_id FROM files")]
            k = sum(_delete(conn, i) for i in rows if i not in live)
            conn.execute("DELETE FROM blobs WHERE hash NOT IN (SELECT blob_hash FROM segments WHERE blob_hash IS NOT NULL)")
            return k
        n += _locked(conn, orphans)
    return n


class BeetsFS(BeetsPlugin):
    """database_change fires inside beets' open transaction, before the commit, and beets has
    no event after it: so it only collects item ids, indexed when the beet process exits
    (atexit; SIGTERM/SIGHUP become a normal exit; not after a SIGKILL: the next start reconciles)."""

    def __init__(self):
        super().__init__()
        self.conn, self.lib, self.changed = None, None, set()
        self.register_listener("database_change", self.database_change)
        atexit.register(self.flush)
        for s in (signal.SIGTERM, signal.SIGHUP):  # e.g. an ssh drop during `beet import`
            if signal.getsignal(s) is signal.SIG_DFL:
                signal.signal(s, lambda n, _: sys.exit(128 + n))

    def _conn(self):
        if self.conn is None:
            self.conn = open_index(os.path.join(os.path.dirname(config["library"].as_filename()),
                                                "beetsfs-index.db"))
        return self.conn

    def database_change(self, lib, model):
        self.lib = lib
        self.changed.update([model.id] if isinstance(model, Item) else [i.id for i in model.items()])  # Album

    def flush(self):
        ids, self.changed = self.changed, set()
        if ids:
            log.info("beetsfs: indexing %d changed items", len(ids))
            try:
                reconcile(self.lib, self._conn(), sorted(ids))
            except Exception:
                log.warning("beetsfs: reconcile failed", exc_info=True)

    def commands(self):
        cmd = ui.Subcommand("beetsfs", help="reconcile the beetsfs index with the library")
        cmd.func = lambda lib, opts, args: print(reconcile(lib, self._conn()))
        return [cmd]
