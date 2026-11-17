FROM docker.io/library/rust:1-slim-trixie@sha256:cc9edbcadf2c27013a642b1d1bb7504e26b1142d6b5ebe58f64cf2eb2bc128ae AS build
WORKDIR /src
COPY fuse/Cargo.toml fuse/Cargo.lock ./
COPY fuse/src src
RUN cargo build --release --locked

# Same Debian and Python (3.13) as the distroless stage, so the venv runs there unchanged.
FROM docker.io/library/debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS py
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 nano && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
COPY plugin /src/plugin
# Dependencies exactly as in plugin/uv.lock (versions and hashes), the plugin itself non-editable.
RUN UV_PROJECT_ENVIRONMENT=/venv uv sync --project /src/plugin --frozen --no-dev --no-editable \
      --no-cache --no-python-downloads --python /usr/bin/python3
# Runtime extras for the shell-less final stage: nano (its libs per ldd, libncursesw, libtinfo
# and libc, are already in distroless, byte-identical), the terminfo entries for
# `podman exec -it` (TERM=xterm), and the view mountpoint.
RUN mkdir -p /extras/usr/bin /extras/usr/share/terminfo/x /extras/beetsfs \
 && cp /usr/bin/nano /extras/usr/bin/ \
 && cp /usr/share/terminfo/x/xterm /usr/share/terminfo/x/xterm-256color /extras/usr/share/terminfo/x/

FROM gcr.io/distroless/python3-debian13:latest@sha256:60faad2efce94a847f32e2adbcaefeb2e4fd3e3a78075a3abb600904a5e010e9
COPY --from=py /venv /venv
COPY --from=py /extras /
COPY --from=build /src/target/release/beetsfs /usr/local/bin/beetsfs
COPY docker/config /defaults
COPY docker/entrypoint.py /entrypoint.py
# `beet` on PATH is the wrapper that drops all capabilities first
COPY --chmod=755 docker/beet.py /usr/local/bin/beet
ENV PATH=/usr/local/bin:/venv/bin:/usr/bin:/bin BEETSDIR=/config/beets EDITOR=nano PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/usr/bin/python3", "/entrypoint.py"]
