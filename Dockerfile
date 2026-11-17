FROM docker.io/library/rust:1-slim-trixie@sha256:cc9edbcadf2c27013a642b1d1bb7504e26b1142d6b5ebe58f64cf2eb2bc128ae AS build
WORKDIR /src
COPY fuse/Cargo.toml fuse/Cargo.lock ./
COPY fuse/src src
RUN cargo build --release --locked

# fpcalc for the chroma plugin (acoustic fingerprints), built from source: a minimal FFmpeg
# (file input, common audio demuxers/decoders, resampling; nothing autodetected) as static
# libraries, and Chromaprint's fpcalc linked against it with -static, so it runs on distroless.
# Sources pinned by sha256: Chromaprint's from its GitHub release, FFmpeg's checked against
# its release signing key (FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8).
FROM docker.io/library/ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db AS fpcalc
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake xz-utils \
      curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# curl, not ADD: ffmpeg.org often resets connections, and ADD cannot retry
RUN curl -fsSL --retry 8 --retry-all-errors -O https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz \
         -O https://github.com/acoustid/chromaprint/releases/download/v1.6.2/chromaprint-1.6.2.tar.gz \
 && printf '%s\n' \
      '7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3  ffmpeg-8.1.3.tar.xz' \
      'd54ada62c16b2db396d3251ec54fc7a58909c163131a1cfcf7fa6407dbc14360  chromaprint-1.6.2.tar.gz' \
    | sha256sum -c
RUN tar -xf ffmpeg-8.1.3.tar.xz && cd ffmpeg-8.1.3 \
 && ./configure --prefix=/ff --enable-static --disable-shared --disable-programs --disable-doc \
      --disable-autodetect --disable-network --disable-x86asm --disable-everything \
      --enable-protocol=file --enable-swresample \
      --enable-demuxer=mp3,flac,ogg,mov,matroska,wav,aac,asf \
      --enable-parser=mpegaudio,flac,aac,vorbis,opus \
      --enable-decoder=mp3,mp3float,flac,vorbis,opus,aac,alac,wmav2,pcm_s16le,pcm_s24le \
 && make -j"$(nproc)" && make install
RUN tar -xf chromaprint-1.6.2.tar.gz && cd chromaprint-1.6.2 \
 && cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TOOLS=ON -DBUILD_TESTS=OFF \
      -DBUILD_SHARED_LIBS=OFF -DFFT_LIB=kissfft -DFFMPEG_ROOT=/ff -DCMAKE_PREFIX_PATH=/ff \
      -DCMAKE_EXE_LINKER_FLAGS=-static \
 && cmake --build build -j"$(nproc)" \
 && install -D build/src/cmd/fpcalc /out/fpcalc \
 && ! ldd /out/fpcalc && /out/fpcalc -version

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
COPY --from=fpcalc /out/fpcalc /usr/bin/fpcalc
COPY docker/config /defaults
COPY docker/entrypoint.py /entrypoint.py
# `beet` on PATH is the wrapper that drops all capabilities first
COPY --chmod=755 docker/beet.py /usr/local/bin/beet
# `fpcalc` on PATH retries (cloud reads can fail mid-file under load); the binary is /usr/bin/fpcalc
COPY --chmod=755 docker/fpcalc.py /usr/local/bin/fpcalc
ENV PATH=/usr/local/bin:/venv/bin:/usr/bin:/bin BEETSDIR=/config/beets EDITOR=nano PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["/usr/bin/python3", "/entrypoint.py"]
