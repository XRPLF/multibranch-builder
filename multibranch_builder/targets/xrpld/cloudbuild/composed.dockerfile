# Compile a pre-composed xrpld source tree (already merged and conflict-resolved by
# `multibranch-builder compose`). Same conan-download build as xrpld.dockerfile, but the source is
# COPIED from the upload — Cloud Build never clones, merges, or runs claude.
ARG CI_IMAGE=ghcr.io/xrplf/xrpld/nix-ubuntu:sha-2e25435
# ON rewrites every Supported::No amendment in features.macro to Supported::Yes before
# compiling, the same edit the all-amendments CI workflow makes. Required for a network whose
# chain has amendments enabled that the tree does not mark supported, else nodes amendment-block.
ARG FORCE_SUPPORTED=OFF

FROM ${CI_IMAGE} AS build
ARG FORCE_SUPPORTED
USER root
WORKDIR /work
COPY rippled /work/rippled
WORKDIR /work/rippled

# setup-conan + download prebuilt deps (build only a branch's new deps).
RUN cat conan/global.conf >> "$(conan config home)/global.conf" && \
    conan config install conan/profiles/ -tf "$(conan config home)/profiles/" && \
    conan remote add --index 0 --force xrplf https://conan.xrplf.org/repository/conan/
RUN for d in external/*/; do [ -f "$d/conanfile.py" ] && conan export "$d" >/dev/null 2>&1 || true; done
RUN mkdir -p .build && cd .build && \
    conan install .. --output-folder . --profile:all ci --build=missing --build="b2/*" \
      --options:host='&:tests=False' --options:host='&:xrpld=True' \
      --settings:all build_type=Release --conf:all tools.build:jobs="$(nproc)"

# Compile (parallelism memory-capped: rippled TUs need ~3 GB each; *-highcpu is 1 GB/core).
RUN cd .build && \
    if [ "$FORCE_SUPPORTED" = "ON" ]; then \
      MACRO=../include/xrpl/protocol/detail/features.macro && \
      sed -i 's/Supported::No,/Supported::Yes,/g' "$MACRO" && \
      ! grep -q 'Supported::No,' "$MACRO" && \
      echo "FORCE_SUPPORTED=ON: every amendment in features.macro is Supported::Yes"; \
    fi && \
    cmake -DCMAKE_TOOLCHAIN_FILE:FILEPATH=build/generators/conan_toolchain.cmake \
          -DCMAKE_BUILD_TYPE=Release -Dxrpld=ON -Dtests=OFF -Dvalidator_keys=ON \
          -DCMAKE_CXX_FLAGS=-DBOOST_ASIO_HAS_STD_INVOKE_RESULT \
          -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc" .. && \
    JOBS=$(awk -v c="$(nproc)" '/MemTotal/{m=int($2/1024/1024/3); j=(m<c?m:c); print (j<1?1:j)}' /proc/meminfo) && \
    echo "compiling xrpld with $JOBS parallel jobs (mem-capped)" && \
    cmake --build . --target xrpld --parallel "$JOBS" && \
    strip -s xrpld && \
    mkdir -p /out && cp xrpld /out/ && \
    if cmake --build . --target help | grep -qw 'validator-keys'; then \
      cmake --build . --target validator-keys --parallel "$JOBS" && strip -s validator-keys && cp validator-keys /out/; \
    else echo "no validator-keys target on this branch"; fi

FROM ubuntu:jammy AS runtime
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates patchelf && \
    rm -rf /var/lib/apt/lists/*
# Same canonical path as xrpld.dockerfile — xrpld-lab wraps this base image and supplies its
# own entrypoint at /opt/xrpld/bin/xrpld, so no ENTRYPOINT here.
# validator-keys rides along when the branch builds it (validator_keys=ON).
COPY --from=build /out/ /opt/xrpld/bin/
# The nix-based CI image links the binary against a nix glibc interpreter path that
# is absent from this ubuntu runtime, so the binary won't start. Point it at the
# ubuntu interpreter; all shared libs already resolve under /lib/x86_64-linux-gnu.
RUN for b in /opt/xrpld/bin/*; do patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 "$b"; done
