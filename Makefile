# 门诊预约与分诊协作系统 · 常用命令
.PHONY: help install run dev test lint clean rebuild-index docker-build docker-up docker-down

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## 安装依赖
	pip install -r requirements.txt

run:  ## 生产模式启动（无热重载）
	uvicorn app.main:app --host 0.0.0.0 --port 8000

dev:  ## 开发模式启动（热重载）
	uvicorn app.main:app --reload --port 8000

test:  ## 运行约束回归测试
	pytest tests/ -v

lint:  ## 语法与未使用导入检查
	python -m compileall -q app
	python -m pyflakes app 2>/dev/null || true

rebuild-index:  ## 重建知识库索引（需管理员登录）
	@echo "请在管理后台点击「重建索引」，或调用："
	@echo "  curl -X POST -b cookie.txt http://localhost:8000/admin/api/kb/rebuild"

clean:  ## 清理运行数据（谨慎：会删库与索引）
	rm -rf data/index/* clinic.db __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

docker-build:  ## 构建镜像
	docker build -t clinic-triage-agent:latest .

docker-up:  ## 启动容器
	docker compose up -d

docker-down:  ## 停止容器
	docker compose down
