import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def music(tmp_path_factory):
    d = tmp_path_factory.mktemp("fx") / "music"
    script = Path(__file__).resolve().parents[2] / "tests" / "make_fixtures.sh"
    subprocess.run([script, d], check=True)
    yield d
    d.chmod(0o755)  # so tmp cleanup works


def assemble(segs, src_path) -> bytes:
    """The bytes the mount would serve for a list of Seg."""
    out = b""
    with open(src_path, "rb") as f:
        for data, off, n in segs:
            if data is None:
                f.seek(off)
                out += f.read(n)
            else:
                out += data[off:off + n]
    return out
