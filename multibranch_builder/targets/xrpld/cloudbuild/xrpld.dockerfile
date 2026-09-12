# Build xrpld from a branch, matching rippled's own CI so conan DOWNLOADS the prebuilt deps
# from conan.xrplf.org — no recompiling boost/rocksdb/openssl.
#
# Mirrors .github/actions/{setup-conan,build-deps} in XRPLF/rippled:
#   - the same CI container the prebuilt binaries were compiled in, so conan package IDs
#     match → download instead of source-build;
#   - the `ci` profile + conan/global.conf + the `xrplf` remote;
#   - the repo's conan.lock (auto-used) pins recipe revisions to the prebuilt ones;
#   - `--build=missing` downloads the prebuilt common deps and compiles ONLY a branch's NEW
#     deps (e.g. blake3 / wasm / an updated nudb — see xrpl-guides/RIPPLED_MAC.md).
#
# Bump CI_IMAGE when XRPLF/rippled bumps .github/scripts/strategy-matrix/linux.json. That file
# now names build images `ghcr.io/xrplf/xrpld/nix-<distro>:<image_tag>` (the old
# `ghcr.io/xrplf/ci/ubuntu-jammy:gcc-12-*` family predated a C++23 bump — PR #7369's
# `static constexpr` locals in a constexpr fn need gcc-13+, which gcc-12 rejects). The image is
# still conan-based (setup-conan/build-deps run inside it), so this dockerfile is unchanged.
ARG CI_IMAGE=ghcr.io/xrplf/xrpld/nix-ubuntu:sha-2e25435
ARG REPO=https://github.com/XRPLF/rippled.git
ARG BRANCH=develop
# The DatagramMonitor instrumentation (public). Most perf-test branches don't carry it,
# so we fetch + merge it in. DatagramMonitor is mostly NEW files → clean merge, no AI.
# On a real conflict this `git merge` fails the build (escalate to the local AI-resolve).
ARG DATAGRAM_REPO=
ARG DATAGRAM_REF=
# ON compiles every amendment as Supported::Yes. Required for a network whose chain has
# amendments enabled that the branch does not mark supported, else nodes amendment-block.
ARG FORCE_SUPPORTED=OFF

FROM ${CI_IMAGE} AS build
ARG REPO
ARG BRANCH
ARG DATAGRAM_REPO
ARG DATAGRAM_REF
ARG FORCE_SUPPORTED
USER root
WORKDIR /work
RUN git clone "$REPO" rippled && cd rippled && git checkout "$BRANCH" && \
    if [ -n "$DATAGRAM_REPO" ]; then \
      git config user.email perf@xrplf.local && git config user.name "xrpld-perf" && \
      echo ">> merging datagram $DATAGRAM_REPO $DATAGRAM_REF" && \
      git fetch "$DATAGRAM_REPO" "$DATAGRAM_REF" && \
      git merge --no-edit FETCH_HEAD; \
    fi
WORKDIR /work/rippled

# setup-conan: global.conf + the `ci` profile + the xrplf remote at index 0.
RUN cat conan/global.conf >> "$(conan config home)/global.conf" && \
    conan config install conan/profiles/ -tf "$(conan config home)/profiles/" && \
    conan remote add --index 0 --force xrplf https://conan.xrplf.org/repository/conan/
# Export any branch-local external recipes so --build=missing can build the delta. Best-effort:
# recipes that need an explicit --name/--version (e.g. blake3) are a known per-branch tweak.
RUN for d in external/*/; do [ -f "$d/conanfile.py" ] && conan export "$d" >/dev/null 2>&1 || true; done

# build-deps: download prebuilt deps; build only what's missing (the branch's new deps).
# conan.lock in the repo root is picked up automatically.
RUN mkdir -p .build && cd .build && \
    conan install .. \
      --output-folder . \
      --profile:all ci \
      --build=missing \
      --build="b2/*" \
      --options:host='&:tests=False' \
      --options:host='&:xrpld=True' \
      --settings:all build_type=Release \
      --conf:all tools.build:jobs="$(nproc)"

# Compile xrpld. Parallelism is memory-capped: rippled TUs need ~3 GB each and the build
# machines are *-highcpu (1 GB/core), so full nproc OOM-kills cc1plus.
RUN cd .build && \
    if [ "$FORCE_SUPPORTED" = "ON" ]; then \
      MACRO=../include/xrpl/protocol/detail/features.macro && \
      sed -i 's/Supported::No,/Supported::Yes,/g' "$MACRO" && \
      ! grep -q 'Supported::No,' "$MACRO" && \
      echo "FORCE_SUPPORTED=ON: every amendment in features.macro is Supported::Yes"; \
    fi && \
    cmake -DCMAKE_TOOLCHAIN_FILE:FILEPATH=build/generators/conan_toolchain.cmake \
          -DCMAKE_BUILD_TYPE=Release -Dxrpld=ON -Dtests=OFF \
          -DCMAKE_CXX_FLAGS=-DBOOST_ASIO_HAS_STD_INVOKE_RESULT \
          -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc" .. && \
    JOBS=$(awk -v c="$(nproc)" '/MemTotal/{m=int($2/1024/1024/3); j=(m<c?m:c); print (j<1?1:j)}' /proc/meminfo) && \
    echo "compiling xrpld with $JOBS parallel jobs (mem-capped)" && \
    cmake --build . --target xrpld --parallel "$JOBS" && \
    strip -s xrpld

FROM ubuntu:jammy AS runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates patchelf && \
    rm -rf /var/lib/apt/lists/*
# This image is just a carrier: the binary at one canonical path, nothing else. xrpld-lab consumes
# it with `FROM <this image>` and supplies its own entrypoint + config (exactly like up:standalone),
# so no ENTRYPOINT is declared here.
COPY --from=build /work/rippled/.build/xrpld /opt/xrpld/bin/xrpld
# The nix-based CI image links the binary against a nix glibc interpreter path that
# is absent from this ubuntu runtime, so the binary won't start. Point it at the
# ubuntu interpreter; all shared libs already resolve under /lib/x86_64-linux-gnu.
RUN patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 /opt/xrpld/bin/xrpld
