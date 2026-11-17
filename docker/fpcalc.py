#!/usr/bin/python3
"""`fpcalc` with retries, for beets' chroma plugin. On a cloud mount a read can fail mid-file
under load ("fpcalc exited with status 3", the file reads fine on its own); pyacoustid gives up
on any non-zero exit and drops fpcalc's stderr, so retry here and keep the errors in
/tmp/fpcalc-errors.log (`podman exec beetsfs cat /tmp/fpcalc-errors.log`)."""
import subprocess
import sys
import time

for attempt in range(3):
    r = subprocess.run(["/usr/bin/fpcalc", *sys.argv[1:]], capture_output=True)
    if r.returncode == 0:
        break
    try:
        with open("/tmp/fpcalc-errors.log", "ab") as log:
            log.write(b"%s attempt %d exit %d: %s\n%s" % (time.strftime("%F %T").encode(), attempt + 1,
                      r.returncode, " ".join(sys.argv[1:]).encode(errors="surrogateescape"), r.stderr))
    except OSError:
        pass
    time.sleep(1 + attempt)
sys.stdout.buffer.write(r.stdout)
sys.stderr.buffer.write(r.stderr)
sys.exit(r.returncode)
