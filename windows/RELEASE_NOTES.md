# 声年 Windows 免费版 0.3.2

免费、本地优先、自带 API。无声年账号、邀请码、激活或收费套餐。

## 下载与使用

**普通用户只需下载 [Shengnian-0.3.2-Windows-x64.exe](https://github.com/kingkk469/shengnian/releases/download/v0.3.2-windows/Shengnian-0.3.2-Windows-x64.exe)。** 页面下方 GitHub 自动生成的 `Source code` 是开发源码，使用软件请下载上面的 EXE。

1. 双击下载的 EXE，选择一个普通文件夹进行解压，无需另装解压软件。
2. 打开解压后的“声年”文件夹，双击 `声年.exe`。保留整个文件夹，`_internal` 是程序必需的运行环境。
3. 点击主界面的“API 配置”，填写自己的 DeepSeek API Key 并保存。重启后自动读取；SnapAny Key 可选。
4. 点击开始录音，或导入已有录音。本地录音、转写无需 API Key；AI 调用费用由你的服务商账户承担。

支持 Windows 10/11 x64。建议至少 8 GB 内存、10 GB 可用磁盘。内置 Python、CPU PyTorch、ASR/VAD/标点/声纹模型和 FFmpeg，首次使用无需下载模型。

录音和转写在本机运行。使用 AI 总结、卡片等功能时，相关文字直接发送到你配置的 API 服务商。用户数据在 `%LOCALAPPDATA%\VoiceJournal\Data`；不要将包含录音和密钥的个人数据目录发给别人。

## 验证与限制

- 87 项回归通过，2 项平台检查跳过。
- 实际打包 EXE 的主界面、历史窗口、API 保存及新进程读取、压缩音频导入、公开示例离线转写和 192 维声纹推理通过。
- 测试使用隔离数据目录、空模型缓存及公开示例，未上传真实录音或进行付费 API 调用。
- 本次重新封装仅调整下载格式和源码获取说明，应用程序与模型保持已验证的版本；自解压附件通过完整性检查。
- 程序未做代码签名，Windows 可能提示未知发布者。不同电脑的物理麦克风、热插拔和长时间录音仍需实机验证。

## 开源与对应源码

声年业务代码以 MIT 协议免费开源。发布页同时提供应用源码以及以下第三方对应源码；运行软件无需下载这些附件，开发、审计和再分发时请按对应许可证保留：

- `shengnian-0.3.2-source.zip`：本地构建时的应用源码。
- `VoiceJournal-LGPL-Sources-6.11.1.zip`：Qt、PySide6 和 Shiboken6 对应源码。
- `VoiceJournal-FFmpeg-LGPL-Sources-20260728.zip`：FFmpeg 及其依赖对应源码。
- `7z2602-src.7z`：未修改的 7-Zip 26.02 自解压模块对应源码。
- `SHA256SUMS.txt`：下载文件校验值。

可直接把这个发布页地址发给朋友：https://github.com/kingkk469/shengnian/releases/tag/v0.3.2-windows
