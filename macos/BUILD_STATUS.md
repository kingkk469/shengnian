# Mac 独立应用构建状态

更新时间：2026-09-03。

目标产物：`release/shengnian-macos-arm64.dmg`。包含 `声年.app`、Python、依赖库、FFmpeg 和 4 个本地语音模型；使用者无需安装开发环境。

已经写好：

- `macos/build.sh`：依赖安装、回归测试、原生 FFmpeg 编译、模型准备、PyInstaller 构建、冻结应用自检、DMG 制作、磁盘映像校验与只读挂载检查。
- `macos/shengnian.spec` 与 `src/mac_entry.py`：Apple Silicon 应用封装、麦克风权限描述、后台子进程入口。
- `src/mac_frozen_check.py`：直接运行打包后的应用，检查文件锁、解码器、VAD、ASR/声纹真实推理、主窗口和历史窗口。
- `.github/workflows/macos-app.yml`：GitHub `macos-14` / arm64 构建任务，上传 DMG 和验证报告。
- Mac 界面中的 API 配置窗口，以及仅在用户允许麦克风访问后启动录音的权限处理。

用户已明确授权向现有仓库推送独立构建分支并运行 Mac 构建。适配代码已推送至 `kingkk469/shengnian` 的 `codex/macos-app-20260903` 分支，未修改主分支。

当前阻塞：已连接 GitHub 应用在创建 `.github/workflows/macos-app.yml` 时返回 HTTP 403 `Resource not accessible by integration`。GitHub CLI 已登录仓库管理员账号，但 OAuth 凭证未包含 `workflow` scope。启动官方 `gh auth refresh --hostname github.com --scopes workflow` 授权流程被自动审批拒绝，理由是新增 scope 会扩大账户持久访问权限，现有的推送和构建授权尚不覆盖凭证权限升级。需要用户明确同意新增这一项权限，并完成 GitHub 官方授权流程；没有尝试绕过拒绝。

工作流文件目前仅在本地，尚未启动远程 Mac 构建，尚未产出独立 `.app` 或 `.dmg`。本地检查为 80 项通过、2 项 POSIX 文件锁检查待 Mac 执行。无需用户提供 Mac 参与自动化测试。

仓库未配置 Apple 开发者签名和公证凭证。构建脚本可先产生临时签名的应用；这不等于 Apple 公证，安装说明已区分。

正式构建成功后，需要用实际产物路径、体积、SHA256、Mac 自动化结果替换本页的待构建状态。
