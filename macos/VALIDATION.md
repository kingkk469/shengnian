# Mac 适配与验证记录

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

在 Windows 运行项目回归与新增测试：**81 项通过，2 项因需要 POSIX 平台跳过**。

在 GitHub 苹果芯片 Mac（`macos-14`、Python 3.12.10）完成依赖安装和全套回归：**83 项通过，耗时 3.10 秒**。证据：[构建 33758548504](https://github.com/kingkk469/shengnian/actions/runs/33758548504) 的 `Run regression tests on Mac` 步骤。该轮后续模型下载因切换更新的构建版本主动取消，不代表安装包已通过。

Mac 实测发现并修复了历史录音路径被强制转换为 Windows 分隔符的问题；修正两项测试对临时目录符号链接和本机 FFmpeg 的环境依赖。相同环境中的原生 FFmpeg 9.0.1 编译也已通过。

- 数据目录、解释器路径、中文/空格/引号路径、文件打开参数、Qt 环境选择。
- Mac CPU 推理默认值、API 配置优先级、非法配置处理、API 保存与权限逻辑。
- Windows 当前进程检测、`caffeinate` 启停流程（模拟子进程）、自启 plist 参数往返。
- GUI 冒烟测试：在 Windows Qt 的 offscreen 模式模拟 Mac 界面分支，创建主窗口和历史窗口，显示一条合成历史记录并生成临时截图。没有启动录音、下载模型或调用 AI。
- Python 编译检查与 Bash 语法检查。
- 通过公开 PyPI 为 `aarch64-apple-darwin` / Python 3.12 解析 125 个固定依赖版本，产出 `macos/requirements-resolved.txt`，并已在 Mac 构建机安装成功；模型推理单独作为冻结应用验收步骤。

测试复用了本机已有的 `D:\voice-journal\.venv\Scripts\python.exe` 依赖环境；没有改动原声年安装或生产数据。GUI 测试使用临时目录与示例配置。

## 独立应用验收

最终构建 [33759616541](https://github.com/kingkk469/shengnian/actions/runs/33759616541) 成功，代码提交 `43464c9`。直接运行生成的 `声年.app/Contents/MacOS/Shengnian`，结果如下：

- 基础检查通过（1.59 秒）：arm64 冻结程序、配置落在用户目录、FFmpeg 9.0.1、yt-dlp 2026.08.19 内部入口、VAD 和跨进程锁。
- 模型检查通过（62.32 秒，包含冷启动和加载）：公开示例 WAV 转为 FLAC，再用包内 FFmpeg 解码为 16 kHz 单声道 WAV，成功生成中文转写和 192 维声纹向量。该时长不是持续转写性能基准。
- 原生界面检查通过（4.63 秒）：Qt 平台为 `cocoa`，主窗口和历史窗口均可打开；已取回并查看实际截图。
- 应用签名验证、DMG 完整性校验、只读挂载、应用程序快捷入口和安装说明检查、挂载后应用签名验证均通过。

自动验收仅使用公开模型示例与隔离数据目录，没有上传用户录音、声纹、笔记或 API Key，也没有调用付费 AI API。

完整安装包在 GitHub 可下载；本地已保存自检 JSON、截图和日志。因下载网络过慢，完整 DMG 未保存至本机，不声称完成了本地 DMG 的 SHA256 比对。

## 未包含的硬件与外部服务验收

- 真实麦克风录音与系统麦克风授权交互。
- 持续转写性能、内存压力与不同 Mac 硬件兼容性。
- 真实休眠唤醒、设备热插拔和长时间稳定性。
- 登录自启、Finder 打开文件、Mac 上公开链接抓取。
- Apple 开发者签名和公证。

已启用 GitHub 苹果芯片 Mac 构建机；上述尚未验证项目不会标记为已通过。最新独立应用构建与产物状态见 [BUILD_STATUS.md](BUILD_STATUS.md)。
