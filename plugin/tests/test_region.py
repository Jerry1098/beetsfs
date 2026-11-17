import mutagen.id3

from beetsplug.beetsfs import audio_region


def region(p):
    with open(p, "rb", buffering=0) as f:
        return audio_region(f, bytes(p), p.stat().st_size)


def test_mp3(music):
    p = music / "tagged.mp3"; s, e = region(p)
    assert s == mutagen.id3.ID3(p).size           # full ID3v2 tag incl. header
    assert e == p.stat().st_size - 128            # ID3v1 dropped
    assert p.read_bytes()[s] == 0xFF              # MPEG frame sync


def test_bare_mp3(music):
    p = music / "bare.mp3"
    assert region(p) == (0, p.stat().st_size)
