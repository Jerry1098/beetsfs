import os
import shutil

import pytest
from beets.library import Item, Library
from beetsplug.beetsfs import BeetsFS, open_index, reconcile
from conftest import assemble


@pytest.fixture
def lib(music, tmp_path):
    lib = Library(str(tmp_path / "lib.db"), directory=b"/mnt/beetsfs")
    for n in ("tagged.mp3", "tagged.flac", "plain.ogg"):
        lib.add(Item.from_path(bytes(music / n)))
    return lib


@pytest.fixture
def conn(tmp_path):
    return open_index(str(tmp_path / "index.db"))


@pytest.fixture
def listeners(monkeypatch):
    """beets' global listener registry, emptied for one test (a BeetsFS registers itself)."""
    from collections import defaultdict
    from beets.plugins import BeetsPlugin
    monkeypatch.setattr(BeetsPlugin, "_raw_listeners", defaultdict(list))
    monkeypatch.setattr(BeetsPlugin, "listeners", defaultdict(list))


@pytest.fixture
def plugin(conn, listeners):
    """The plugin registered for beets' real events, on conn."""
    p = BeetsFS(); p.conn = conn
    return p


def rows(conn):
    return conn.execute("SELECT item_id, vpath, size, mtime, meta_hash FROM files ORDER BY item_id").fetchall()


def segs_of(conn, item_id):
    out = []
    for h, off, n in conn.execute("SELECT blob_hash, offset, len FROM segments WHERE item_id=? ORDER BY seq", (item_id,)):
        data = None if h is None else conn.execute("SELECT data FROM blobs WHERE hash=?", (h,)).fetchone()[0]
        out.append((data, off, n))
    return out


def write_tmp(tmp_path, data):
    p = tmp_path / "out.mp3"
    p.write_bytes(data)
    return bytes(p)


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_full_reconcile(lib, conn):
    assert reconcile(lib, conn) == 3
    for item in lib.items():
        id_, vpath, size, _, _ = conn.execute("SELECT item_id, vpath, size, mtime, meta_hash FROM files WHERE item_id=?", (item.id,)).fetchone()
        assert vpath == item.destination(relative_to_libdir=True)
        assert size == len(assemble(segs_of(conn, id_), item.path))


def test_unchanged_is_noop(lib, conn):
    reconcile(lib, conn); before = rows(conn)
    assert reconcile(lib, conn) == 0 and rows(conn) == before


def test_edit_updates_row(lib, conn, tmp_path):
    reconcile(lib, conn); item = lib.items("title:wrong").get()
    old = conn.execute("SELECT vpath, meta_hash FROM files WHERE item_id=?", (item.id,)).fetchone()
    item.title = "Right"; item.store()
    assert reconcile(lib, conn, [item.id]) == 1
    assert conn.execute("SELECT vpath, meta_hash FROM files WHERE item_id=?", (item.id,)).fetchone() != old
    assert Item.from_path(write_tmp(tmp_path, assemble(segs_of(conn, item.id), item.path))).title == "Right"


def test_removed_item_and_orphan_blobs(lib, conn):
    reconcile(lib, conn); lib.items().get().remove()
    assert reconcile(lib, conn) == 1
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM blobs WHERE hash NOT IN (SELECT blob_hash FROM segments WHERE blob_hash IS NOT NULL)").fetchone()[0] == 0


def test_identical_bytes_stored_once(lib, conn, music, tmp_path):
    # same file twice with the same tags -> identical blobs, stored once
    reconcile(lib, conn); one = count(conn, "blobs")
    lib.add(Item.from_path(bytes(music / "tagged.mp3")))
    reconcile(lib, conn)
    assert count(conn, "blobs") == one


def test_path_collision(lib, conn, music):
    a = lib.add(Item.from_path(bytes(music / "tagged.mp3")))
    reconcile(lib, conn)
    vpaths = [r[1] for r in rows(conn)]
    assert len(set(vpaths)) == len(vpaths)
    assert any(v.endswith(b" [%d].mp3" % a) for v in vpaths)


def test_non_utf8_name(lib, conn, music, tmp_path):
    p = tmp_path / b"caf\xe9.mp3".decode("utf-8", "surrogateescape"); shutil.copy(music / "tagged.mp3", p)
    item = Item.from_path(os.fsencode(p)); lib.add(item)
    reconcile(lib, conn)
    assert conn.execute("SELECT src_path FROM files WHERE item_id=?", (item.id,)).fetchone()[0] == os.fsencode(p)


def test_missing_source_is_kept_not_fatal(lib, conn, music, tmp_path):
    # a cloud mount that is down at start must not empty the view: only leaving the library does
    p = tmp_path / "gone.mp3"; shutil.copy(music / "tagged.mp3", p); item = Item.from_path(bytes(p)); lib.add(item)
    reconcile(lib, conn); p.unlink()
    assert reconcile(lib, conn) == 0  # must not raise
    assert count(conn, "files") == 4
    item.remove()
    assert reconcile(lib, conn) == 1 and count(conn, "files") == 3


def test_events(lib, conn, plugin):
    reconcile(lib, conn)
    item = lib.items("title:wrong").get(); item.title = "Evt"; item.store()
    old = conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]
    assert b"Evt" not in old  # collected only: beets may not have committed yet
    plugin.flush()  # at exit
    assert b"Evt" in conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]
    # item.remove() fires database_change once the row is gone (the source still exists)
    item.remove(); plugin.flush()
    assert conn.execute("SELECT 1 FROM files WHERE item_id=?", (item.id,)).fetchone() is None
    assert count(conn, "files") == 2


def test_events_from_another_thread(lib, tmp_path, listeners):
    # beets fires database_change from importer threads; the index is beetsfs-index.db next to
    # the library and is written at exit, from the main thread
    import threading
    from beets import config
    old, config["library"] = config["library"].get(), str(tmp_path / "lib.db")
    plugin = BeetsFS()
    try:
        reconcile(lib, plugin._conn())
        assert os.path.exists(tmp_path / "beetsfs-index.db")
        item = lib.items("title:wrong").get(); item.title = "Thread"; item.store()
        t = threading.Thread(target=plugin.database_change, args=(lib, item)); t.start(); t.join()
        plugin.flush()
        assert b"Thread" in plugin._conn().execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]
    finally:
        plugin._conn().close()
        config["library"] = old


def test_bad_item_does_not_abort(lib, conn, monkeypatch):
    bad = lib.items("title:wrong").get()
    orig = Item.destination

    def dest(self, *a, **k):
        if self.id == bad.id:
            raise RuntimeError("boom")
        return orig(self, *a, **k)

    monkeypatch.setattr(Item, "destination", dest)
    assert reconcile(lib, conn) == 2
    assert {r[0] for r in rows(conn)} == {i.id for i in lib.items()} - {bad.id}


def test_writer_gets_in_during_a_long_reconcile(lib, conn, tmp_path, monkeypatch):
    # another writer must not wait for the scan: the lock is only held while a changed item is
    # written, not while its segments are built (slow: network reads)
    import sqlite3
    import threading
    from beetsplug import beetsfs
    orig, got = beetsfs.build_segments, []

    def writer():
        w = sqlite3.connect(str(tmp_path / "index.db"), timeout=5, isolation_level=None)
        w.execute("BEGIN IMMEDIATE"); w.execute("COMMIT"); w.close(); got.append(1)

    def slow(item):  # the writer runs, and must finish, while a build is in progress
        if not got:
            t = threading.Thread(target=writer); t.start(); t.join(10)
        return orig(item)
    monkeypatch.setattr(beetsfs, "build_segments", slow)
    assert reconcile(lib, conn) == 3
    assert got


def chain_path(conn, dir_id, name):
    parts = [name]
    while dir_id != 1:
        dir_id, n = conn.execute("SELECT parent, name FROM dirs WHERE id=?", (dir_id,)).fetchone()
        parts.append(n)
    return b"/".join(reversed(parts))


def empty_dirs(conn):
    return count(conn, "dirs WHERE id != 1 AND id NOT IN (SELECT dir_id FROM files) AND id NOT IN (SELECT parent FROM dirs)")


def test_dirs_chain(lib, conn):
    reconcile(lib, conn)
    assert conn.execute("SELECT * FROM dirs WHERE id=1").fetchone() == (1, 0, b"")
    for vpath, d, name in conn.execute("SELECT vpath, dir_id, name FROM files").fetchall():
        assert chain_path(conn, d, name) == vpath
    assert empty_dirs(conn) == 0


def test_moved_and_removed_items_prune_dirs(lib, conn, plugin):
    reconcile(lib, conn)
    item = lib.items("title:wrong").get(); item.artist = "Elsewhere"; item.store(); plugin.flush()
    assert empty_dirs(conn) == 0
    assert b"Elsewhere" in chain_path(conn, *conn.execute("SELECT dir_id, name FROM files WHERE item_id=?", (item.id,)).fetchone())
    for i in lib.items():
        i.remove()
    plugin.flush()
    assert conn.execute("SELECT * FROM dirs").fetchall() == [(1, 0, b"")]


@pytest.mark.parametrize("bad", [b"/abs/x.mp3", b"a/../x.mp3", b"a//x.mp3", b"./x.mp3", b"a/x\0.mp3", b"a/"])
def test_hostile_vpath_left_out(lib, conn, monkeypatch, bad):
    reconcile(lib, conn); item = lib.items("title:wrong").get()
    monkeypatch.setattr(Item, "destination", lambda self, *a, **k: bad if self.id == item.id else b"ok/%d.mp3" % self.id)
    reconcile(lib, conn)
    assert {r[0] for r in rows(conn)} == {i.id for i in lib.items()} - {item.id}
    assert empty_dirs(conn) == 0



def test_item_added_before_orphan_sweep_survives(lib, conn, music, monkeypatch):
    # an import whose event writes its row after the scan, before the orphan sweep takes the lock
    from beetsplug import beetsfs
    orig, added = beetsfs._locked, []

    def locked(conn, f, *args):
        if f.__name__ == "orphans" and not added:
            item = Item.from_path(bytes(music / "tagged.flac")); lib.add(item); added.append(item.id)
            reconcile(lib, conn, [item.id])
        return orig(conn, f, *args)
    monkeypatch.setattr(beetsfs, "_locked", locked)
    reconcile(lib, conn)
    assert conn.execute("SELECT 1 FROM files WHERE item_id=?", (added[0],)).fetchone()


def test_full_scan_does_not_undo_a_modify(lib, conn, monkeypatch):
    from beetsplug import beetsfs
    snapshot = list(lib.items())  # the scan's lib.items(), taken before the modify
    item = lib.items("title:wrong").get(); item.title = "New"; item.store()
    reconcile(lib, conn, [item.id])  # the modify's event
    new = conn.execute("SELECT vpath, meta_hash FROM files WHERE item_id=?", (item.id,)).fetchone()
    monkeypatch.setattr(lib, "items", lambda *a: snapshot)
    reconcile(lib, conn)
    assert conn.execute("SELECT vpath, meta_hash FROM files WHERE item_id=?", (item.id,)).fetchone() == new

    # the event lands between the scan's check and its write: the scan's (older) row is not written
    conn.execute("DELETE FROM files")
    item.title = "Newer"; item.store()
    orig = beetsfs.build_segments

    def slow(it):
        if it.id == item.id and item.title == "Newer":
            item.title = "Newest"; item.store(); reconcile(lib, conn, [item.id])
        return orig(it)
    monkeypatch.setattr(beetsfs, "build_segments", slow)
    reconcile(lib, conn)
    assert b"Newest" in conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]


def test_scan_with_a_stale_path_keeps_a_moved_item(music, tmp_path, monkeypatch):
    lib = Library(str(tmp_path / "lib2.db"), directory=bytes(tmp_path / "lib"))
    p = tmp_path / "a.mp3"; shutil.copy(music / "tagged.mp3", p); item = Item.from_path(bytes(p)); lib.add(item)
    conn = open_index(str(tmp_path / "index2.db")); reconcile(lib, conn)
    snapshot = list(lib.items())  # the scan begins
    item.move(); item.store(); reconcile(lib, conn, [item.id])  # `beet move` elsewhere, and its flush
    monkeypatch.setattr(lib, "items", lambda *a: snapshot)
    reconcile(lib, conn)  # reaches the stale item: old path gone
    assert conn.execute("SELECT src_path FROM files WHERE item_id=?", (item.id,)).fetchone() == (bytes(item.path),)


def test_read_error_is_retried_not_stored(lib, conn, monkeypatch):
    import builtins
    item = lib.items("title:wrong").get()
    real_open = builtins.open

    def eio(p, *a, **k):
        if p == bytes(item.path):
            raise OSError(5, "EIO (network)")
        return real_open(p, *a, **k)
    monkeypatch.setattr(builtins, "open", eio)
    reconcile(lib, conn)
    assert conn.execute("SELECT 1 FROM files WHERE item_id=?", (item.id,)).fetchone() is None  # skipped
    monkeypatch.setattr(builtins, "open", real_open)
    assert reconcile(lib, conn) == 1  # retried
    assert count(conn, "segments WHERE item_id=%d" % item.id) > 1  # retagged, not a passthrough


def test_fresh_import_is_not_rewritten(music, tmp_path, plugin, conn):
    # beets holds comp=False in memory at import, 0 once read back: the index uses the stored item
    lib = Library(str(tmp_path / "lib3.db"), directory=b"/mnt/beetsfs")
    item = Item.from_path(bytes(music / "tagged.mp3")); item.comp = False; lib.add(item)
    plugin.database_change(lib, item); plugin.flush()
    assert reconcile(lib, conn) == 0


def test_mtime_moves_on_every_change(lib, conn):
    reconcile(lib, conn); item = lib.items("title:wrong").get()
    m = [conn.execute("SELECT mtime FROM files WHERE item_id=?", (item.id,)).fetchone()[0]]
    for t in ("A", "B"):  # within one second
        item.title = t; item.store(); reconcile(lib, conn, [item.id])
        m.append(conn.execute("SELECT mtime FROM files WHERE item_id=?", (item.id,)).fetchone()[0])
    assert m[0] < m[1] < m[2]


def test_stale_build_does_not_overwrite_a_newer_copy(lib, conn, monkeypatch):
    # A builds from item X; X changes in the library meanwhile: A must not write its old copy
    # (the reconcile that saw the change writes the new one)
    from beetsplug import beetsfs
    reconcile(lib, conn); item = lib.items("title:wrong").get()
    item.title = "One"; item.store()
    orig = beetsfs.build_segments

    def slow(it):
        if it.title == "One":
            item.title = "Two"; item.store()  # committed while A builds
        return orig(it)
    monkeypatch.setattr(beetsfs, "build_segments", slow)
    assert reconcile(lib, conn, [item.id]) == 0  # A: skipped
    assert b"One" not in conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]
    assert reconcile(lib, conn, [item.id]) == 1  # B
    assert b"Two" in conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0]


def test_path_only_change_reads_no_source(lib, conn, monkeypatch):
    from beetsplug import beetsfs
    reconcile(lib, conn); item = lib.items("title:wrong").get()
    segs = segs_of(conn, item.id)
    monkeypatch.setattr(beetsfs, "build_segments", lambda it: pytest.fail("source read"))
    monkeypatch.setattr(Item, "destination", lambda self, *a, **k: b"new/place/%d.mp3" % self.id)
    assert reconcile(lib, conn) == 3
    assert conn.execute("SELECT vpath FROM files WHERE item_id=?", (item.id,)).fetchone()[0] == b"new/place/%d.mp3" % item.id
    assert segs_of(conn, item.id) == segs and empty_dirs(conn) == 0


def test_real_beet_indexes_at_exit_also_after_sighup(music, tmp_path):
    # the real CLI: the import is indexed when beet exits; a modify that gets SIGHUP after its
    # commit (an ssh drop) is indexed all the same
    import subprocess
    import sys
    d = tmp_path / "beets"; d.mkdir()
    (d / "config.yaml").write_text(f"directory: {tmp_path / 'view'}\nlibrary: {tmp_path / 'library.db'}\n"
                                   "import: {copy: no, move: no, write: no}\nplugins: [beetsfs]\n")
    env = {**os.environ, "BEETSDIR": str(d)}

    def beet(code):
        r = subprocess.run([sys.executable, "-c", "from beets import ui\n" + code], env=env, capture_output=True, text=True)
        return r.returncode, r.stderr
    rc, err = beet(f"ui.main(['import', '-A', '-q', {str(music / 'tagged.mp3')!r}])")
    assert rc == 0, err
    idx = open_index(str(tmp_path / "beetsfs-index.db"))
    assert count(idx, "files") == 1
    rc, err = beet("""import os, signal, beets.plugins as p
real = p.send
def send(ev, **k):
    if ev == "cli_exit": os.kill(os.getpid(), signal.SIGHUP)
    return real(ev, **k)
p.send = send
ui.main(["modify", "-y", "title=Hup"])""")
    assert rc == 128 + 1, err
    assert b"Hup" in idx.execute("SELECT vpath FROM files").fetchone()[0], err
