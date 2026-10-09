# 声年 Windows 免费版

[GitHub 下载页](https://github.com/kingkk469/shengnian/releases/tag/v0.3.2-windows)：下载 `Shengnian-0.3.2-Windows-x64.exe`，双击并选择目录解压；再运行“声年”文件夹中的 `声年.exe`。无需另外安装解压软件。第三方对应源码作为发布页的独立附件提供。

Windows 10/11 x64 的独立便携版，版本 0.3.2。内置 Python、CPU PyTorch、四个语音模型和 FFmpeg；解压整个 ZIP 后双击 `声年.exe`，在主界面“API 配置”填写自己的 DeepSeek Key。

本地交付入口为 `../assets-heavy/windows-0.3.2/clean/发给朋友/`，实际文件在 E 盘。无需声年账号、激活或邀请码。API 费用由使用者自己的服务商账户承担。

开发构建需 Python 3.12、PyInstaller 6.16、项目依赖，以及 CPU 版 torch。当前包使用 PySide6 / Shiboken6 6.11.1、torch 2.5.1+cpu。`build.ps1` 的参数需分别指向 Python、全新输出目录、四个模型的父目录、FFmpeg shared 目录和已保存的 LGPL 对应源码目录；大文件输出应在 E 盘。

完整构建流程：PyInstaller → 依赖与模型清单 → 隔离用户目录中的冻结程序检查 → 附带应用及第三方源码 → ZIP 归档和完整性检查。测试仅使用公开模型示例及合成 Key，不发送真实录音或调用付费 API。

验证范围：主界面、历史窗口、API 保存与新进程读取、FFmpeg 压缩音频导入、ASR / VAD / 标点 / 声纹推理。物理麦克风、热插拔与长期录音仍需在接收者机器上验证。EXE 当前未做代码签名。

默认 API 模型沿用 `deepseek-v4-flash`。2026-10-09 查询 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/codex)：该名称仍接受，调用由现行 Flash 模型处理。本次未使用真实 Key 进行收费调用，验证范围是配置保存、加载和本地功能。
