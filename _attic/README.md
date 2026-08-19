# _attic — 暂存区

项目未启用 git，硬删不可恢复，因此零引用的源码文件先移到这里，确认不需要后可整目录删除。

- `ifc_parser.py`（原 backend/services/ifc_parser.py，2026-08-19 清理）：IFC BIM 模型解析，
  全项目零引用（无任何 API/Agent/脚本 import）。若后续做 BIM 审图功能可取回。
- `_gov_struct.py`（原 scripts/_gov_struct.py）：一次性结构抓取辅助，零引用。
