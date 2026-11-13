# What the image is built on. The default carries CUDA for an NVIDIA GPU;
# cuDNN 9 is in it on purpose: CTranslate2 (under faster-whisper) links
# against it, and the plain CUDA runtime image fails at model load with an
# error that names no library at all.
#
# The CPU-only image (the -cpu tags) is this same file on plain Ubuntu:
#
#     docker build --build-arg BASE=ubuntu:22.04 -f docker/Dockerfile .
#
# CTranslate2 only loads the CUDA libraries when a GPU is asked for, so
# without them Whisper runs on the CPU exactly as it does in the default image
# on a machine with no GPU - and the image is a fraction of the size. Keep the
# Ubuntu release the same as the CUDA base's, so both are the same Python and
# the same ffmpeg.
ARG BASE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/config/cache/huggingface \
    CENSARR_CONFIG=/config \
    CENSARR_CACHE=/config/cache \
    CENSARR_WEB=/app/web

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt /app/requirements.txt
# Ubuntu 22.04's pip (22.0) crashes in its resolver - AssertionError in
# get_topological_weights - on current releases of these packages' dependencies.
RUN pip3 install --no-cache-dir --upgrade "pip>=24" \
    && pip3 install --no-cache-dir -r /app/requirements.txt

COPY server/censarr /app/censarr
COPY web /app/web

EXPOSE 8477
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "uvicorn", "censarr.main:app", "--host", "0.0.0.0", "--port", "8477"]
