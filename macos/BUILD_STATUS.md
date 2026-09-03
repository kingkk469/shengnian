# Mac 独立应用构建状态

更新时间：2026-09-03。

状态：**独立应用与 DMG 已构建成功，并通过 Mac 自动验收**。包含 `声年.app`、Python、依赖库、FFmpeg 和 4 个本地语音模型；使用者无需安装开发环境。

[GitHub Release 下载页](https://github.com/kingkk469/shengnian/releases/tag/v0.3.1-macos-beta.1)。发布版使用 macOS 原生两卷磁盘映像：下载 `shengnian-macos-arm64.dmg` 和 `shengnian-macos-arm64.002.dmgpart` 到同一文件夹，双击 `.dmg` 即可安装。无需登录 GitHub，不受 Actions 临时产物保留期限制。

两卷由下面已验证的原始 DMG 生成，未重新构建或删减模型。发布前重新检查原始 SHA256、原生分卷完整性、只读挂载和包内应用深度签名；全部附件上传后逐个核对 GitHub 记录的大小与 SHA256，再公开版本。

Release 已于 2026-09-03 22:14（北京时间）公开，ID 为 `382077337`。第一卷 1,573,080,326 字节，第二卷 825,974,818 字节，总计约 2.40 GB。发布云任务 `33764943842` 的原生分卷验收和六个附件上传完成；最后一步因 GitHub 不通过标签接口返回未发布草稿而中断，已修正为按 release ID 读取草稿，并使用保存的独立校验记录完成发布。六个附件的大小、SHA256、中文说明和标签提交均已读回确认。发布记录保存在本地 `release/publication/`。

原始构建产物记录（以下摘要对应构建 ZIP，分卷校验请使用 Release 附件 `SHA256SUMS.txt`）：

- 下载 ZIP：2,446,999,099 字节，约 2.45 GB（2.28 GiB）。
- ZIP 的 SHA256：`eacd9fea05e933b2242ab234b6e0969ad11679e468422f775db94c01e5567f9a`，来自 GitHub 产物元数据。
- ZIP 包含 DMG 及其 `.sha256` 校验文件；上面的摘要对应整个 ZIP，不是内部 DMG。
- [完整构建记录](https://github.com/kingkk469/shengnian/actions/runs/33759616541)。实际应用代码提交为 `43464c9`。
- Mac 自检报告和截图已保存到同一 Release 的 `macos-verification.zip` 和 `shengnian-mac.png`。
- 发布流程：`.github/workflows/macos-release.yml` 和 `macos/publish_release.py`，发布标签 `v0.3.1-macos-beta.1` 指向应用构建提交 `43464c9`。

已经写好：

- `macos/build.sh`：依赖安装、回归测试、原生 FFmpeg 编译、模型准备、PyInstaller 构建、冻结应用自检、DMG 制作、磁盘映像校验与只读挂载检查。
- `macos/shengnian.spec` 与 `src/mac_entry.py`：Apple Silicon 应用封装、麦克风权限描述、后台子进程入口。
- `src/mac_frozen_check.py`：直接运行打包后的应用，检查文件锁、解码器、VAD、ASR/声纹真实推理、主窗口和历史窗口。
- `.github/workflows/macos-app.yml`：GitHub `macos-14` / arm64 构建任务，上传 DMG 和验证报告。
- Mac 界面中的 API 配置窗口，以及仅在用户允许麦克风访问后启动录音的权限处理。

用户已明确授权向现有仓库推送独立构建分支并运行 Mac 构建。适配代码已推送至 `kingkk469/shengnian` 的 `codex/macos-app-20260903` 分支，未修改主分支。

用户已单独授权增加 GitHub CLI 的 `workflow` scope，并已在 GitHub 官方网页完成确认。权限已验证生效，工作流已随提交 `4ede70a` 推送到独立分支。

首轮 Mac 实测暴露的路径问题已修复。第二轮 Mac 全套回归 **83 项通过**，原生 FFmpeg 编译通过；模型下载期间主动取消该轮，以切换包含资源缓存、实际解码自检与原生界面检查的更新版本。

最终构建 `33759616541` 全部通过：Mac 回归、原生解码器构建、模型准备、应用封装、实际音频解码、中文转写、192 维声纹、Cocoa 主窗口和历史窗口、应用签名检查、DMG 完整性校验及只读挂载后的应用检查。Windows 检查为 81 项通过、2 项跳过；两项 POSIX 检查已在 Mac 通过。

应用使用 ad-hoc 签名，尚未经过 Apple 开发者公证。Mac 云主机没有验证真实麦克风、设备热插拔、合盖睡眠和长时间录音；这些限制不标记为已通过。

本地 `release/macos-arm64/` 已保存安装说明、实际 Mac 截图与 `evidence/` 验证报告。完整 DMG 的本地备份下载因网络过慢已停止，没有把不完整下载当成成品；完整安装包请从上面的 GitHub 入口获取。原 1.8 MB 源码 ZIP 保留为历史产物，不是本次独立安装包。
