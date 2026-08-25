# =============================================================================
# Railway Inspection AR Glasses — Top-level Makefile
# =============================================================================
# 顶层构建入口，整合眼镜固件、挎包终端、协议测试、SaaS 扩展。
# 依赖: ESP-IDF v5.1 (idf.py), Python 3.11+, Docker, psql
# =============================================================================

# 默认 Shell
SHELL := /bin/bash

# 项目根目录
PROJECT_ROOT := $(shell pwd)

# 眼镜固件目录
GLASSES_DIR := $(PROJECT_ROOT)/glasses-firmware
# 挎包终端目录
BAG_DIR := $(PROJECT_ROOT)/bag-terminal
# 共享协议目录
PROTOCOL_DIR := $(PROJECT_ROOT)/shared/protocols
# SaaS 扩展目录
SAAS_EXT_DIR := $(PROJECT_ROOT)/saas-extensions

# ESP-IDF 环境激活脚本 (可通过环境变量覆盖)
IDF_PATH ?= $(shell echo $$IDF_PATH)
IDF_EXPORT ?= $(IDF_PATH)/export.sh

# Python 解释器
PYTHON ?= python3

# Docker
DOCKER ?= docker
DOCKER_COMPOSE ?= docker compose

# 颜色输出
COLOR_RESET := \033[0m
COLOR_GREEN  := \033[0;32m
COLOR_YELLOW := \033[0;33m
COLOR_BLUE   := \033[0;34m

# 目标
.PHONY: all help glasses glasses-flash glasses-monitor bag bag-up bag-down \
        protocol-test protocol-clean saas-ext submodules clean

all: protocol-test glasses bag
	@echo -e "$(COLOR_GREEN)=== 所有组件构建完成 ===$(COLOR_RESET)"

## help: 显示所有可用目标
help:
	@echo "铁路巡检智能眼镜 — 嵌入式 Monorepo 构建系统"
	@echo ""
	@echo "可用目标:"
	@echo "  make all              构建所有组件 (协议测试 + 眼镜固件 + 挎包终端)"
	@echo "  make glasses          编译 ESP32-S3 眼镜固件 (ESP-IDF)"
	@echo "  make glasses-flash    烧录眼镜固件到开发板"
	@echo "  make glasses-monitor  烧录并打开串口监控"
	@echo "  make bag              构建挎包终端 Docker 镜像"
	@echo "  make bag-up           启动挎包终端容器"
	@echo "  make bag-down         停止挎包终端容器"
	@echo "  make protocol-test    运行共享协议一致性测试"
	@echo "  make protocol-clean   清理协议测试缓存"
	@echo "  make saas-ext         应用 SaaS 后端 PostgreSQL 扩展"
	@echo "  make submodules       初始化并更新 Git 子模块"
	@echo "  make clean            清理所有构建产物"
	@echo ""
	@echo "环境变量:"
	@echo "  IDF_PATH              ESP-IDF 安装路径 (默认: \$IDF_PATH)"
	@echo "  PYTHON                Python 解释器 (默认: python3)"
	@echo "  ESP_PORT              眼镜固件烧录串口 (默认: /dev/ttyACM0)"

## submodules: 初始化并更新 Git 子模块 (lvgl / esp32-camera / tuya SDK)
submodules:
	@echo -e "$(COLOR_BLUE)=== 初始化 Git 子模块 ===$(COLOR_RESET)"
	git submodule update --init --recursive

## glasses: 编译 ESP32-S3 眼镜固件
glasses:
	@echo -e "$(COLOR_BLUE)=== 编译眼镜固件 ===$(COLOR_RESET)"
	@if [ -z "$$IDF_PATH" ] || [ ! -f "$$(echo $$IDF_PATH)/export.sh" ]; then \
		echo -e "$(COLOR_YELLOW)警告: IDF_PATH 未设置或无效，尝试直接调用 idf.py$(COLOR_RESET)"; \
		source $(IDF_EXPORT) 2>/dev/null && cd $(GLASSES_DIR) && idf.py build || { \
			echo -e "$(COLOR_YELLOW)请先激活 ESP-IDF 环境: . \$IDF_PATH/export.sh$(COLOR_RESET)"; exit 1; }; \
	else \
		source $(IDF_EXPORT) && cd $(GLASSES_DIR) && idf.py build; \
	fi

## glasses-flash: 烧录眼镜固件
glasses-flash: glasses
	@echo -e "$(COLOR_BLUE)=== 烧录眼镜固件 ===$(COLOR_RESET)"
	source $(IDF_EXPORT) && cd $(GLASSES_DIR) && idf.py -p $(ESP_PORT) flash

## glasses-monitor: 烧录并监控串口
glasses-monitor: glasses
	@echo -e "$(COLOR_BLUE)=== 烧录并监控眼镜固件 ===$(COLOR_RESET)"
	source $(IDF_EXPORT) && cd $(GLASSES_DIR) && idf.py -p $(ESP_PORT) flash monitor

## bag: 构建挎包终端 Docker 镜像
bag:
	@echo -e "$(COLOR_BLUE)=== 构建挎包终端 Docker 镜像 ===$(COLOR_RESET)"
	cd $(BAG_DIR) && $(DOCKER) build -t railway-bag-terminal:latest .

## bag-up: 启动挎包终端容器
bag-up: bag
	@echo -e "$(COLOR_BLUE)=== 启动挎包终端容器 ===$(COLOR_RESET)"
	cd $(BAG_DIR) && $(DOCKER_COMPOSE) up -d

## bag-down: 停止挎包终端容器
bag-down:
	@echo -e "$(COLOR_BLUE)=== 停止挎包终端容器 ===$(COLOR_RESET)"
	cd $(BAG_DIR) && $(DOCKER_COMPOSE) down

## protocol-test: 运行共享协议一致性测试
protocol-test:
	@echo -e "$(COLOR_BLUE)=== 运行协议一致性测试 ===$(COLOR_RESET)"
	cd $(PROJECT_ROOT) && $(PYTHON) $(PROTOCOL_DIR)/test_consistency.py

## protocol-clean: 清理协议测试缓存
protocol-clean:
	@echo -e "$(COLOR_BLUE)=== 清理协议测试缓存 ===$(COLOR_RESET)"
	rm -rf $(PROTOCOL_DIR)/__pycache__ $(PROJECT_ROOT)/shared/__pycache__
	find $(PROTOCOL_DIR) -name "*.pyc" -delete

## saas-ext: 应用 SaaS 后端 PostgreSQL 扩展
saas-ext:
	@echo -e "$(COLOR_BLUE)=== 应用 SaaS 后端扩展 ===$(COLOR_RESET)"
	@if [ -z "$$DATABASE_URL" ]; then \
		echo -e "$(COLOR_YELLOW)警告: DATABASE_URL 未设置，使用默认连接$(COLOR_RESET)"; \
		psql -h localhost -U postgres -d site_mgmt -f $(SAAS_EXT_DIR)/02-extensions.sql; \
	else \
		psql "$$DATABASE_URL" -f $(SAAS_EXT_DIR)/02-extensions.sql; \
	fi

## clean: 清理所有构建产物
clean:
	@echo -e "$(COLOR_BLUE)=== 清理所有构建产物 ===$(COLOR_RESET)"
	rm -rf $(GLASSES_DIR)/build $(GLASSES_DIR)/managed_components
	rm -rf $(BAG_DIR)/.pytest_cache $(BAG_DIR)/htmlcov
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
