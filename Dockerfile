# KaoyanBench v1.1 —— 精简镜像（零运行时依赖）
# ---------------------------------------------------------------------------
# 说明：本项目核心链路纯标准库实现，因此镜像只需 Python 运行时 + 源码。
#       ★ 这是「把 CLI 打包进容器」的部署手段，**不等于** Sandbox 的 Docker 隔离。
#         v1.0 的沙箱隔离级别是「进程级」（方案 R10 / docs/06 §11），docker 沙箱为 P2 占位。
# 用法：
#   docker build -t kaoyanbench:1.1.0 .
#   docker run --rm kaoyanbench:1.1.0 kaoyanbench validate --suite core50
#   docker run --rm kaoyanbench:1.1.0 kaoyanbench run --suite smoke --agent mock --tag docker-smoke
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# 零第三方运行时依赖；不装任何额外包以保持镜像精简。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    KAOYANBENCH_ROOT=/app

WORKDIR /app

# 先只拷贝打包所需文件，利用层缓存（源码变更不重复装依赖；本项目无依赖，仍保持清晰）。
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# editable 安装（生成 kaoyanbench / kyb 两个入口命令）
RUN python -m pip install --upgrade pip && \
    python -m pip install -e .

# 拷贝运行所需的数据资产与配置（事实源）
COPY config/ ./config/
COPY benchmark/ ./benchmark/
COPY tools/ ./tools/

# 运行产物目录（建议构建时留空，运行时用 -v 挂载宿主目录以保留产物）
RUN mkdir -p /app/results /app/reports

# 以非 root 运行（更安全；容器内仍为进程级隔离）
RUN useradd -m -u 10001 kyb && chown -R kyb:kyb /app
USER kyb

# 默认命令：跑一次离线校验（无网络、无密钥）
CMD ["kaoyanbench", "validate", "--suite", "core50"]
