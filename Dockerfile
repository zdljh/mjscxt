# ==========================================================================
# 漫剧生成系统 · 后端容器镜像（P2-7）
#
# 设计说明（重要）：
#   ComfyUI（Qwen-Image / MiniMax H3 / FlashVSR）依赖本机 GPU 与数十 GB 模型权重，
#   **不放入本镜像**，由宿主机或独立 GPU 容器运行；
#   本镜像只容器化 Flask 后端 + 业务依赖，通过 COMFYUI_URL 连接引擎。
#
# 构建： docker build -t mjscxt-backend:2.0 .
# 运行： docker run -p 5000:5000 -e COMFYUI_URL=http://host.docker.internal:8188 mjscxt-backend:2.0
# ==========================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# ffmpeg：成片合成 / 超分后处理 / 配音混音必需
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存：代码改动不会触发重装依赖）
COPY app/requirements.txt /app/app/requirements.txt
RUN pip install -r /app/app/requirements.txt

# 再拷贝源码（.dockerignore 已排除产物/密钥/大文件）
COPY app /app/app
COPY locales /app/locales
COPY Dockerfile /app/Dockerfile

# 产物目录（容器内持久化挂载点）
RUN mkdir -p /app/output /app/novels /app/output/projects

# 非 root 运行
RUN useradd -m -u 1000 mjscxt && chown -R mjscxt:mjscxt /app
USER mjscxt

ENV COMFYUI_URL=http://host.docker.internal:8188 \
    DEPLOY_PROFILE=server \
    FLASK_RUN_PORT=5000 \
    PYTHONPATH=/app/app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/api/tasks >/dev/null || exit 1

WORKDIR /app/app
CMD ["python", "-c", "import os; from app import app; app.run(host='0.0.0.0', port=int(os.getenv('FLASK_RUN_PORT','5000')), threaded=True)"]
