# Revit MCP 适配器

该目录包含用于连接本机 Revit Routes 服务的适配器，源码来自
`mcp-server-for-revit-python`，按 MIT 许可保留在本目录的 `LICENSE`。

它必须运行在安装了 Revit 和 pyRevit Routes 的 Windows 主机上，不能放进 Linux
后端容器。后端与 Revit 通过项目内 `data/runtime/revit` 交换 `model.json` 和 RVT
产物；容器部署时由 Compose 把该目录挂载进去。

调试时使用项目根目录的 `requirements.txt` 安装依赖，或配置
`REVIT_MCP_PYTHON` 指向独立虚拟环境。pyRevit 脚本修改后运行
`scripts/sync_revit_ext.py` 同步部署副本。
