# 门诊预约与分诊协作系统 · 生产镜像
FROM python:3.11-slim

# 中文字体与时区，避免 PDF 解析与时间显示问题
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

# 先装依赖再拷代码，充分利用构建缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY templates ./templates
COPY data ./data

# 非 root 运行（等保要求）
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /srv/app
USER appuser

EXPOSE 8000

# 健康检查：容器编排据此判断存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
