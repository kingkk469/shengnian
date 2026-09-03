# 声年 Mac 独立应用 · 测试版 1

适用于 **苹果芯片 Mac（M1 或更新）和 macOS 14+**。内置 Python、FFmpeg 和四个本地语音模型，无需安装开发环境或另外下载模型。

## 下载和安装

请下载下面 **两个文件**，放到同一个文件夹，保留原文件名：

1. [shengnian-macos-arm64.dmg](https://github.com/kingkk469/shengnian/releases/download/v0.3.1-macos-beta.1/shengnian-macos-arm64.dmg)
2. [shengnian-macos-arm64.002.dmgpart](https://github.com/kingkk469/shengnian/releases/download/v0.3.1-macos-beta.1/shengnian-macos-arm64.002.dmgpart)

下载完后，**双击 `shengnian-macos-arm64.dmg`**，把“声年”拖入“Applications（应用程序）”。Mac 会自动读取第二个文件，无需手动合并或使用终端。

完整安装包约 2.45 GB；由于 GitHub 每个附件必须小于 2 GiB，使用 macOS 原生分卷磁盘映像。两卷缺一不可。页面底部的 `Source code` 是源码，安装应用请用上面的两个文件。

从“应用程序”启动声年。首次录音时按系统提示允许麦克风访问。录音和本地转写不需要 API Key；AI 总结可在“API 配置”中填写自己的 DeepSeek Key。

本版为 ad-hoc 签名，尚未经过 Apple 开发者公证。若首次启动被拦截，请在“系统设置 → 隐私与安全性”中按系统提示确认打开。

## 验证情况

- Mac 回归测试 83 项通过。
- 独立应用实际完成压缩音频解码、中文转写、192 维声纹推理。
- Cocoa 主窗口和历史窗口检查通过。
- 原始及分卷磁盘映像完整性、只读挂载和包内应用签名检查通过。
- 真实麦克风、热插拔、合盖睡眠和长时间录音尚未做硬件验收。

应用构建提交：`43464c93b24679beb612e1db29e7832658125775`。
[查看原始 Mac 构建记录](https://github.com/kingkk469/shengnian/actions/runs/33759616541)。验证报告和截图已作为此版本附件保存。

![实际 Mac 界面](https://github.com/kingkk469/shengnian/releases/download/v0.3.1-macos-beta.1/shengnian-mac.png)
