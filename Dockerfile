# ============================================================
# aftersale-cs 应用镜像（可选：不想要容器化 API 可跳过本文件）
# 构建：docker build -t aftersale-cs .
# 依赖 docker-compose.yml 里的 redis-stack 服务
# ============================================================
FROM python:3.12-slim

WORKDIR /app

# 先拷依赖清单装包（利用 Docker 层缓存，改代码不重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 数据目录（知识库/向量库/订单库）保持本地化
EXPOSE 8000

# 启动前可自测：python -c "import cs_supervisor" 确认导入
CMD ["uvicorn", "api_server:api", "--host", "0.0.0.0", "--port", "8000"]