# ☁️ 爱快自动备份 (iKuai Auto Backup to CD2)

![Docker Pulls](https://img.shields.io/badge/docker%20pulls-jiumianex%2Fikuai--backup-blue)
![Platform](https://img.shields.io/badge/platform-Docker-success)
![License](https://img.shields.io/badge/license-MIT-green)

> **“数据安全最重要”** 这是一个专为 [爱快 (iKuai)](https://www.ikuai8.com/) 路由器设计的轻量级、全自动配置备份工具。它支持定时将路由器的系统配置导出，并通过 gRPC 协议极速推送到 [CloudDrive 2 (CD2)](https://www.clouddrive2.com/) 挂载的各大网盘中。

项目配备了极致优雅的 **Apple (macOS / iOS) 风格 Web 控制台**，内置纯代码绘制的精美矢量图标，让自动化运维也能拥有顶级的视觉体验。

---

## ✨ 核心功能

- **🤖 全自动工作流**：登录路由 -> 触发备份 -> 本地下载 -> 登录 CD2 -> 推送网盘 -> 销毁本地临时文件，一气呵成。
- **⏱️ 灵活的定时调度**：内置 APScheduler 引擎，支持通过标准的 `Cron` 表达式自定义备份频率（如每天凌晨 3 点执行）。
- **🗑️ 智能生命周期管理**：自动识别 CD2 云端目标目录，并清理超过指定“保留天数”的陈旧备份文件，为您节省网盘空间。
- **🎨 极致优雅的 Web UI**：
  - 纯正的苹果风格：内置 iCloud 蓝绿渐变 SVG 矢量图标。
  - 基于高斯模糊 (Glassmorphism) 的登录界面。
  - Apple 控制面板风格的参数表单与 iOS 仿生开关。
  - **高度还原的 macOS 极客终端**，实时输出后端日志，告别枯燥的黑底白字。
- **🐳 纯粹的容器化**：无数据库依赖，所有配置文件与运行日志均持久化在 `json` 和 `log` 文件中，完美适配 Docker 部署。

---

## 🚀 安装教程 (Docker Compose)

推荐使用 Docker Compose 进行部署，只需几步即可运行。

### 1. 创建挂载目录与配置文件
在你服务器的合适位置新建一个文件夹，并创建 `docker-compose.yml` 文件：

```bash
mkdir -p /opt/ikuai-backup/data
cd /opt/ikuai-backup
nano docker-compose.yml
