# LGPL 组件替换说明

PySide6、Shiboken6、Qt 与音频组件以独立 DLL/PYD 动态加载。可以备份整个程序目录后，用相同架构、ABI 兼容的自行构建版本替换以下目录中的组件，然后运行 `声年.exe`：

- `_internal/PySide6/`
- `_internal/shiboken6/`
- `_internal/soxr/`
- `_internal/tools/` 中的 FFmpeg 可执行文件及动态库

软件不通过账号或强制哈希检查阻止这些替换。自行修改版本的兼容性需自行验证。完整对应源码放在 `legal/sources/`。
