"""Container entrypoint (PID 1). Everything runs as container root (rootless podman: that is
the host user). The image has no shell: stdlib only."""
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading

# The daemon mounts one level below the view volume: consumers bind the volume (a plain
# directory), so a stop/start or a SIGKILL never leaves them holding a copy of a dead mount.
MNT, INDEX = "/beetsfs/music", "/config/beets/beetsfs-index.db"  # the plugin's: next to library.db
MNT_DETACH = 2
libc = ctypes.CDLL(None, use_errno=True)


def log(msg):
    print(msg, file=sys.stderr, flush=True)


daemon = None


def reconcile():  # once per start, in a daemon thread: PID 1's exit ends it and a running `beet`
    # catches what the beet commands' own exit-time indexing missed (killed commands, changed sources)
    try:
        if (rc := subprocess.call(["beet", "beetsfs"])) != 0:
            log(f"beetsfs: reconcile exited ({rc})")
    except OSError as e:
        log(f"beetsfs: reconcile: {e}")


def unmount():
    """Lazily unmount our FUSE mount at MNT if it is there (top of MNT is fuse with source
    "beetsfs"; mountinfo never stats it). Lazy: a consumer may hold a file in the view. The
    volume is rshared, so this clears the view path outside the container too."""
    top = [l.split(" - ")[1].split()[:2] for l in open("/proc/self/mountinfo") if l.split()[4] == MNT][-1:]
    if not (top and top[0][0].startswith("fuse") and top[0][1] == "beetsfs"):
        return False
    if libc.umount2(MNT.encode(), MNT_DETACH) != 0:
        log(f"beetsfs: could not unmount {MNT}: {os.strerror(ctypes.get_errno())}")
        return False
    return True


# `podman stop` sends SIGTERM: the daemon exits (it has no capabilities left to unmount) and the
# main thread unmounts after its wait(). Installed first: PID 1 drops signals it has no handler for.
term = False


def on_signal(*_):
    global term
    term = True
    if daemon:
        daemon.terminate()


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

# A stale mount from a SIGKILLed container (`podman start` / restart policy): the volume brings
# the dead fuse mount along at MNT, and anything touching MNT fails until it is gone.
if unmount():
    log("beetsfs: cleared a stale mount")

if not os.path.exists("/config/beets/config.yaml"):
    os.makedirs("/config/beets", exist_ok=True)
    shutil.copy("/defaults/beets/config.yaml", "/config/beets/")
os.makedirs(MNT, exist_ok=True)

daemon = subprocess.Popen(["beetsfs", INDEX, MNT])
if term:  # signalled before the daemon existed
    daemon.terminate()
threading.Thread(target=reconcile, daemon=True).start()

rc = daemon.wait()
unmount()  # also after a crash or SIGKILL of the daemon alone: consumers see an empty view, not a dead one
if term:
    sys.exit(143)
rc = 128 - rc if rc < 0 else rc  # killed by a signal: report it like a shell does
log(f"beetsfs daemon exited ({rc}), stopping the container")
sys.exit(rc)
