# Mac 测试版 1 适配记录

日期：2026-09-03。来源：声年开源版 0.3.0，本地工作目录 `D:\king项目\_inbox\shengnian-open-source`。

## 已实现

- 拆出 `src/platform_support.py`：用户数据路径、解释器选择、文件打开、Qt 平台、API 配置加载、推理设备、进程检测、POSIX 单实例锁和 Mac 录音防休眠。
- Mac 启动不再强制加载 Qt 的 Windows 插件；界面适配屏幕尺寸、中文字体和多选提示。
- 后台录音/转写通过项目 `.venv/bin/python` 启动；原有 Windows `.venv/Scripts/python.exe` 路线保留。
- Mac 录音和转写分别持有操作系统文件锁。接管后台服务时同时检查锁、PID 和进程启动时间；不扫描并杀死无关进程。
- POSIX 停止信号转为可清理的退出流程；录音长时间无回调会重新连接。
- 使用 `caffeinate -i -w PID` 限制录音过程的空闲休眠，退出或停止录音后释放。
- 补齐 Mac FFmpeg、yt-dlp、Obsidian 与 DJI 挂载目录路径。
- 提供安装、启动、配置、自检和可选登录自启脚本。API 文件按 `0600` 写入，现有环境变量优先。
- 修正默认 Obsidian 仓库占位值为空；已有用户配置不覆盖。

## 已执行的验证

在 Windows 运行项目回归与新增测试：**79 项通过，2 项因需要 POSIX 平台跳过**。

- 数据目录、解释器路径、中文/空格/引号路径、文件打开参数、Qt 环境选择。
- Mac CPU 推理默认值、API 配置优先级、非法配置处理、API 保存与权限逻辑。
- Windows 当前进程检测、`caffeinate` 启停流程（模拟子进程）、自启 plist 参数往返。
- GUI 冒烟测试：在 Windows Qt 的 offscreen 模式模拟 Mac 界面分支，创建主窗口和历史窗口，显示一条合成历史记录并生成临时截图。没有启动录音、下载模型或调用 AI。
- Python 编译检查与 Bash 语法检查。
- 通过公开 PyPI 为 `aarch64-apple-darwin` / Python 3.12 解析 125 个固定依赖版本，产出 `macos/requirements-resolved.txt`。PyTorch、torchaudio、PySide6、NumPy、SciPy、WebRTC VAD 与 rookiepy 要求有目标平台预编译包；jieba 等依赖允许源码分发。此步骤没有在 Mac 上执行安装或模型推理。

测试复用了本机已有的 `D:\voice-journal\.venv\Scripts\python.exe` 依赖环境；没有改动原声年安装或生产数据。GUI 测试使用临时目录与示例配置。

## 尚未验证

- 实际 macOS / Cocoa 窗口与麦克风授权。
- 苹果芯片环境中的完整依赖安装、ASR 和声纹推理、速度与内存。
- 两项 POSIX 锁测试、真实休眠唤醒、设备热插拔和长时间稳定性。
- 登录自启、Finder 打开文件、Mac 上公开链接抓取。
- `.app` / `.dmg` 构建、签名和公证。

本轮没有可用 Mac 主机，因此这些项目保持待验收状态。交付是源码测试包，不标记为已验证的 Mac 成品。
