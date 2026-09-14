# `gau-unraid` VM Docker Toolchain Bootstrap Specification

`gau-unraid` VM (`192.168.22.202`) 作为专用的构建、测试与演练主机，其环境初始化必须满足**版本锁定 (Pinned)**、**校验和验证 (Checksum Verified)** 与 **执行幂等 (Idempotent)** 的要求。

---

## 1. 基础运行环境

- **Host**: `192.168.22.202` (`gau-unraid`)
- **User**: `ying` (UID 1000)
- **Docker 模式**: Rootless Docker
- **Docker Socket**: `unix:///run/user/1000/docker.sock`
- **Docker CLI 路径**: `/home/ying/bin/docker`

### 环境变量调用规范
自动化工具（如 `docker_hygiene.py`、CI 脚本、构建命令）通过 SSH 连接执行 Docker 命令时，必须在命令前缀显式注入环境，**严禁依赖对 VM 用户 `~/.bashrc` 或全局环境的不可逆持久篡改**：

```bash
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
export DOCKER_HOST="unix:///run/user/1000/docker.sock"
```

---

## 2. 锁定插件版本与校验和 (Pinned Toolchain)

所有 CLI 插件安装至 `~/.docker/cli-plugins/`：

### 2.1 Docker Buildx

| 属性 | 锁定配置 |
|---|---|
| **插件名称** | `docker-buildx` |
| **锁定版本** | `v0.37.1` |
| **官方发布源** | `https://github.com/docker/buildx/releases/download/v0.37.1/buildx-v0.37.1.linux-amd64` |
| **SHA-256 校验和** | `9447199cdb435f25880548343c128a4b6650e8891ee598905d8d29d39a8e359b` |
| **安装位置** | `/home/ying/.docker/cli-plugins/docker-buildx` |
| **权限** | `chmod +x` |

### 2.2 Docker Compose

| 属性 | 锁定配置 |
|---|---|
| **插件名称** | `docker-compose` |
| **锁定版本** | `v5.5.1` |
| **官方发布源** | `https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64` |
| **SHA-256 校验和** | `db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576` |
| **安装位置** | `/home/ying/.docker/cli-plugins/docker-compose` |
| **权限** | `chmod +x` |

---

## 3. 幂等初始化命令 (Idempotent Bootstrap)

可以通过 `scripts/docker_hygiene.py bootstrap-gau` 自动执行，也可以使用如下幂等 shell 脚本：

```bash
set -euo pipefail

mkdir -p ~/.docker/cli-plugins

# 1. buildx
BUILDX_SHA="9447199cdb435f25880548343c128a4b6650e8891ee598905d8d29d39a8e359b"
BUILDX_TARGET="$HOME/.docker/cli-plugins/docker-buildx"
if [ ! -f "$BUILDX_TARGET" ] || ! echo "$BUILDX_SHA  $BUILDX_TARGET" | sha256sum -c --status 2>/dev/null; then
    echo "[*] Installing pinned buildx v0.37.1..."
    curl -fsSL -o "$BUILDX_TARGET" https://github.com/docker/buildx/releases/download/v0.37.1/buildx-v0.37.1.linux-amd64
    echo "$BUILDX_SHA  $BUILDX_TARGET" | sha256sum -c -
    chmod +x "$BUILDX_TARGET"
fi

# 2. compose
COMPOSE_SHA="db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576"
COMPOSE_TARGET="$HOME/.docker/cli-plugins/docker-compose"
if [ ! -f "$COMPOSE_TARGET" ] || ! echo "$COMPOSE_SHA  $COMPOSE_TARGET" | sha256sum -c --status 2>/dev/null; then
    echo "[*] Installing pinned compose v5.5.1..."
    curl -fsSL -o "$COMPOSE_TARGET" https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64
    echo "$COMPOSE_SHA  $COMPOSE_TARGET" | sha256sum -c -
    chmod +x "$COMPOSE_TARGET"
fi

echo "[*] Bootstrap verified."
```

---

## 4. 共享构建缓存策略 (Approved Shared Cache)

- **Default Builder**: `default` (driver: docker)
- **缓存软上限 (Soft Limit)**: 15 GB
- **缓存硬上限 (Hard Limit)**: 25 GB
- **淘汰策略**: 达到硬上限时，执行 LRU 清理：`docker buildx prune --keep-storage 15GB`
