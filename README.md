# 声年

声年是一个本地优先的 AI 语音知识库：持续录音、本地中文转写，再用你自己的 AI API 生成总结、待办、会议纪要和知识卡片。Windows 版已有验证；苹果芯片 Mac 独立应用已完成构建，并通过 Mac 自动化转写、声纹和原生界面检查。

**Mac 用户从 [Mac 独立应用安装说明](macos/README.md) 开始。** 安装包内置运行时和模型，适用于苹果芯片、macOS 14+；尚未经过 Apple 公证，物理麦克风和长时间录音仍未做硬件验收。

这个仓库是免费开源版：没有声年账号、邀请码、激活、套餐、购买、退款或产品 Token 网关。你需要自行申请并承担 AI API 服务商产生的费用。

## 核心能力

- 麦克风常驻录音，自动静音切片（Mac 麦克风权限和稳定性待实机验证）
- FunASR 本地中文转写，原始音频不发送给声年服务器
- 用户自有 DeepSeek API 生成每日总结、复盘、待办和卡片
- 本地历史管理、声纹识别、会议纪要和可选 Obsidian 输出
- 导入本地音频；部分公开网页链接可抓取并转写

## 隐私边界

录音、转写、声纹、热词、笔记、卡片和日志默认只在你的电脑上。只有你主动使用 AI 功能时，完成该任务所需的文字才会直接发送到你配置的 API 服务商。声年开源版没有中转服务器。

## 系统要求

- Windows 10/11，或苹果芯片 Mac / macOS 14+
- Python 3.12（Mac 独立应用已内置）
- 8 GB 内存起步，建议预留至少 5 GB 磁盘空间
- FFmpeg（导入 MP3、M4A 等格式时需要，Mac 独立应用已内置）
- 麦克风；DJI Mic 2 或普通 USB/内置麦克风均可

## 快速安装

```powershell
git clone https://github.com/kingkk469/shengnian.git
cd shengnian
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

安装依赖需要一些时间。FunASR、PyTorch 和语音模型体积较大；第一次转写时还会下载模型。

申请 DeepSeek API Key 后，在 PowerShell 里写入当前 Windows 用户环境变量：

```powershell
setx DEEPSEEK_API_KEY "sk-你的-Key"
```

关闭并重新打开终端，然后启动：

```powershell
.\start-launcher.bat
```

也可以直接运行：

```powershell
.\.venv\Scripts\python.exe .\src\launcher.py
```

## 配置

首次运行 `setup.ps1` 会自动从 `src/config.example.toml` 创建本机的 `src/config.toml`。这个文件已被 Git 忽略。

默认设置可以直接启动：

- 用户数据：`%LOCALAPPDATA%\VoiceJournal\Data`
- AI：DeepSeek 直连
- 笔记：声年本地目录；如需 Obsidian，可填写 `[obsidian].vault`
- 麦克风：优先匹配 DJI Mic；找不到时尝试 Realtek
- 本人声纹名：默认是 `我`；如果改名，请同步修改 `[speaker].owner_name`

其他可用环境变量：

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek AI 功能，必填 |
| `SNAPANY_API_KEY` | 可选，视频号公开链接解析 |
| `VOICE_JOURNAL_DATA_ROOT` | 可选，覆盖用户数据目录 |
| `VOICE_JOURNAL_AI_SHADOW_METERING=1` | 可选，在本机记录无正文的 API 用量统计 |

## 数据目录

```text
VoiceJournal/Data/
├── raw/              原始录音
├── transcripts/      本地转写
├── notes/            总结、卡片和运行结果
├── meetings/         会议纪要
├── runtime/          声纹、状态和本地索引
└── logs/             运行日志
```

这些目录以及 API Key、Cookie、本机配置均不会被 Git 跟踪。Mac 配置向导将自有 API Key 保存在数据目录的 `runtime/api-keys.json`，权限为仅当前用户可读写；显式设置的环境变量优先。不要在 Issue 中上传真实录音、转写、凭证文件或日志原文。

## 常用命令

```powershell
# 只启动图形界面
.\.venv\Scripts\python.exe .\src\launcher.py

# 手动总结今天
.\.venv\Scripts\python.exe .\src\daily_summary.py

# 查看可用录音设备
.\.venv\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"

# 安装开机自启（管理员 PowerShell）
.\install-autostart.ps1
```

## 已知限制

- 当前只正式验证 Windows 和 Python 3.12。
- Mac 源码测试版以苹果芯片、macOS 14+、Python 3.12 为首轮测试目标；默认 CPU 转写，尚未确认 MPS 加速、持续录音和声纹模型的实机表现。
- 第一次下载 FunASR 模型可能较慢。
- 公开平台链接抓取会受平台页面变化影响；默认不读取浏览器 Cookie。
- AI 输出可能出错，涉及决策、发布或对外材料时请人工核对。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m compileall src
.\.venv\Scripts\python.exe -m pytest
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [SECURITY.md](SECURITY.md)。

跨平台改造与本轮验证记录见 [Mac 适配验证记录](macos/VALIDATION.md)。

## 开源协议

[MIT License](LICENSE) © 2026 数字生命 King

## 致谢

- [FunASR](https://github.com/modelscope/FunASR)
- [ModelScope](https://modelscope.cn/)
- [DeepSeek](https://www.deepseek.com/)
- [PySide6](https://doc.qt.io/qtforpython-6/)
