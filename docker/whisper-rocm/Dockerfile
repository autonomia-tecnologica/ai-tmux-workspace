# whisper.cpp built for ROCm (HIP), only for the RX 7600 family (gfx1102). Offline at run time.
#   docker build -t at-whisper-rocm docker/whisper-rocm
# Other AMD GPU: docker build --build-arg GPU_TARGET=gfx1100 ...  (find yours with `rocminfo | grep gfx`)
FROM rocm/dev-ubuntu-24.04:latest
ARG WHISPER_REF=v1.9.4
ARG GPU_TARGET=gfx1102
RUN apt-get update \
 && apt-get install -y --no-install-recommends git cmake build-essential hipblas-dev rocblas-dev \
 && git clone --depth 1 --branch ${WHISPER_REF} https://github.com/ggml-org/whisper.cpp /src \
 && cmake -S /src -B /src/build -DGGML_HIP=ON -DAMDGPU_TARGETS=${GPU_TARGET} -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER=/opt/rocm/llvm/bin/clang -DCMAKE_CXX_COMPILER=/opt/rocm/llvm/bin/clang++ \
      -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON \
 && cmake --build /src/build -j"$(nproc)" --target whisper-cli \
 && cp /src/build/bin/whisper-cli /usr/local/bin/ \
 && find /src/build -name '*.so*' -exec cp -P {} /usr/local/lib/ \; && ldconfig \
 && rm -rf /src /var/lib/apt/lists/*
ENTRYPOINT ["whisper-cli"]
