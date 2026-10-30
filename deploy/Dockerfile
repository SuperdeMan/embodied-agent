ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}
ARG RELEASE_SHA=development
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    MUJOCO_GL=osmesa PYOPENGL_PLATFORM=osmesa \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 LP_NUM_THREADS=1 \
    EMBODIED_RELEASE=${RELEASE_SHA}
# 仅在镜像内安装离屏渲染依赖，不改变宿主系统。 / Install headless rendering dependencies only inside the image.
RUN apt-get update && apt-get install -y --no-install-recommends libosmesa6 libgl1 libglib2.0-0 \
    && python -m pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple uv==0.11.2
WORKDIR /app
COPY . /app
RUN uv sync --frozen --no-dev --group sim \
    && mkdir -p /app/outputs \
    && chown -R 10001:10001 /app/outputs
USER 10001:10001
EXPOSE 8390
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8390/healthz', timeout=2)"]
ENTRYPOINT ["/app/.venv/bin/embodied"]
CMD ["console", "--host", "0.0.0.0", "--port", "8390", "--provider", "offline"]
