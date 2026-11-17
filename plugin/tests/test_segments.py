import shutil
from pathlib import Path

import mutagen
import pytest
from beets.library import Item
from beetsplug import beetsfs
from beetsplug.beetsfs import ZEROS, build_segments
from conftest import assemble
from test_region import region

REAL = Path(__file__).resolve().parents[2] / "tests" / "music"


def cover_bytes(p):
    f = mutagen.File(p)
    return f.pictures[0].data if hasattr(f, "pictures") else f.tags.getall("APIC")[0].data


def retag(path):
    item = Item.from_path(bytes(path))
    item.update(dict(title="Right", artist="Fixed", album="Album", track=7, year=1999))
    return item


def check_matches(src, tmp_path):
    item = retag(src)
    virt = tmp_path / src.name
    segs = build_segments(item)
    virt.write_bytes(assemble(segs, src))
    ref = tmp_path / ("ref" + src.suffix)
    shutil.copy(src, ref)
    ref.chmod(0o644)
    item.write(path=bytes(ref))  # what beets does to a writable copy
    a, b = Item.from_path(bytes(virt)), Item.from_path(bytes(ref))
    assert all(a.get(k) == b.get(k) for k in Item._media_tag_fields)
    assert a.title == "Right" and a.track == 7
    return segs


@pytest.mark.parametrize("name", ["tagged.mp3", "bare.mp3", "tagged.flac"])
def test_matches_real_write(music, tmp_path, name):
    check_matches(music / name, tmp_path)


@pytest.mark.parametrize("name", ["tagged.mp3", "tagged.flac"])
def test_audio_and_cover_come_from_source(music, name):
    src = music / name
    segs = build_segments(retag(src))
    raw = src.read_bytes()
    s, e = region(src)
    assert segs[-1] == (None, s, e - s)  # audio untouched
    cover = cover_bytes(src)
    assert (None, raw.find(cover), len(cover)) in segs  # cover referenced
    assert stored(segs) < 4096  # stored bytes are small


def stored(segs):
    return sum(n for d, _, n in segs if d is not None and d is not ZEROS)


@pytest.mark.parametrize("name", ["tagged.mp3", "bare.mp3", "tagged.flac"])
def test_padding_not_stored(music, name):
    segs = build_segments(retag(music / name))
    assert any(d is ZEROS for d, _, _ in segs)  # mutagen's padding points into the shared blob
    assert not any(d[o:o + n].endswith(b"\0" * 64) for d, o, n in segs if d is not None and d is not ZEROS)


def test_huge_head_falls_back(music, monkeypatch):
    src = music / "tagged.flac"
    monkeypatch.setattr(beetsfs, "MAX_HEAD", 100)
    assert build_segments(retag(src)) == [(None, 0, src.stat().st_size)]


@pytest.mark.parametrize("name", ["plain.ogg", "id3.flac", "garbage.flac", "empty.mp3"])
def test_passthrough(music, name):
    src = music / name
    assert build_segments(Item(path=bytes(src))) == [(None, 0, src.stat().st_size)]


def test_write_failure_falls_back(music, monkeypatch):
    src = music / "tagged.mp3"
    item = retag(src)
    monkeypatch.setattr(Item, "write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    assert build_segments(item) == [(None, 0, src.stat().st_size)]


def test_moved_audio_falls_back(music, monkeypatch):
    # a writer that corrupts the stub's tail must be detected by the K-byte check
    src = music / "tagged.flac"
    item = retag(src)

    def bad(self, path=None, **k):
        with open(path, "ab") as f:
            f.write(b"x")

    monkeypatch.setattr(Item, "write", bad)
    assert build_segments(item) == [(None, 0, src.stat().st_size)]


@pytest.mark.parametrize(
    "src", sorted(p for p in REAL.rglob("*") if p.suffix in (".mp3", ".flac")) or [None]
)
def test_real_music(src, tmp_path):
    if src is None:
        pytest.skip("tests/music absent")
    segs = check_matches(src, tmp_path)
    assert stored(segs) < 16 * 1024


def test_long_zero_run_before_a_cut(tmp_path):
    # padding longer than ZEROS, then a cover cut from the source, then more tag bytes
    src = tmp_path / "src"; src.write_bytes(b"COVER")
    a, b = b"x" + b"\0" * 150000, b"y" + b"\0" * 100
    segs = []
    beetsfs._blob(segs, a); segs.append((None, 0, 5)); beetsfs._blob(segs, b)
    assert assemble(segs, src) == a + b"COVER" + b
    assert [n for d, _, n in segs if d is ZEROS] == [65536, 65536, 150000 - 2 * 65536, 100]


@pytest.mark.parametrize(
    "src", ["tagged.mp3", "tagged.flac"] + sorted(str(p) for p in REAL.rglob("*") if p.suffix in (".mp3", ".flac")))
def test_cover_is_not_read(music, src, monkeypatch, tmp_path):
    # the network cost of a retag: the tag area minus the pictures, plus K bytes of audio
    import builtins
    src = music / src if "/" not in src else Path(src)
    real_open, read = builtins.open, []

    class Counting:
        def __init__(self, f): self.f = f
        def __enter__(self): return self
        def __exit__(self, *a): self.f.close()
        def __getattr__(self, n): return getattr(self.f, n)
        def read(self, n=-1):
            b = self.f.read(n); read.append(len(b)); return b

    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: Counting(real_open(p, *a, **k)) if p == bytes(src) else real_open(p, *a, **k))
    segs = check_matches(src, tmp_path)  # tags still match a real beet write
    covers = sum(n for d, _, n in segs[:-1] if d is None)
    assert covers > 0 and sum(read) < 64 * 1024, (covers, sum(read))
