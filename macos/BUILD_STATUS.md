# Mac 独立应用构建状态

更新时间：2026-09-03。

目标产物：`release/shengnian-macos-arm64.dmg`。包含 `声年.app`、Python、依赖库、FFmpeg 和 4 个本地语音模型；使用者无需安装开发环境。

已经写好：

- `macos/build.sh`：依赖安装、回归测试、原生 FFmpeg 编译、模型准备、PyInstaller 构建、冻结应用自检和 DMG 制作。
- `macos/shengnian.spec` 与 `src/mac_entry.py`：Apple Silicon 应用封装、麦克风权限描述、后台子进程入口。
- `src/mac_frozen_check.py`：直接运行打包后的应用，检查文件锁、解码器、VAD、ASR/声纹真实推理、主窗口和历史窗口。
- `.github/workflows/macos-app.yml`：GitHub `macos-14` / arm64 构建任务，上传 DMG 和验证报告。
- Mac 界面中的 API 配置窗口，以及仅在用户允许麦克风访问后启动录音的权限处理。

当前阻塞：自动审批拒绝把适配代码推到现有公开 GitHub 仓库，理由是本轮用户尚未明确授权向该公开目的地推送源码。尚未推送、尚未启动远程 Mac 构建，尚未产出独立应用或 DMG。已向用户请求这一项授权；无需用户提供 Mac 参与测试。

已确认本机 GitHub CLI 登录账号为仓库管理员，Actions 功能开启；CLI OAuth 当前未包含 `workflow` scope，新增工作流时还需验证已连接 GitHub 应用是否具备相应权限。未获取任何私密凭证。

仓库未配置 Apple 开发者签名和公证凭证。构建脚本可先产生临时签名的应用；这不等于 Apple 公证，安装说明已区分。

正式构建成功后，需要用实际产物路径、体积、SHA256、Mac 自动化结果替换本页的待构建状态。
