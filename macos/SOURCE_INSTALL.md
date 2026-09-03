# 声年 Mac 源码安装说明

适用于愿意参与测试的苹果芯片 Mac 用户。首轮目标环境为 **macOS 14 或更新版本、原生 arm64 Python 3.12**。Intel Mac 与 Rosetta 环境本轮不作为支持目标。

本页供开发者从源码运行使用。Mac 构建环境的依赖安装和 83 项自动化测试已通过。普通用户请优先查看 [独立应用安装说明](README.md) 和 [构建状态](BUILD_STATUS.md)；源码包需要另行安装依赖。

## 安装

1. 解压整个 `shengnian-mac-beta1` 文件夹，放到自己的固定目录，如 `~/Applications/shengnian-mac-beta1`。请使用新目录，不覆盖原来的声年数据或其他 Python 环境。
2. 按 [Homebrew 官方说明](https://brew.sh/) 安装 Homebrew。已有 Homebrew 可跳过。
3. 打开“终端”，输入 `cd `（末尾有空格），把解压后的文件夹拖进终端，回车。
4. 执行：

```bash
bash setup-macos.command
```

脚本会安装 Homebrew 的 Python 3.12、FFmpeg，创建项目专用 `.venv`，安装 Python 依赖，并创建缺少的默认配置。已有配置不会覆盖。终端如果以 Rosetta 运行，需要先关闭 Rosetta 模式。

Python 依赖使用 `macos/requirements-resolved.txt` 中的固定版本；已针对苹果芯片 Python 3.12 从公开 PyPI 完成 125 个包的依赖解析。主要二进制依赖要求有目标平台预编译包。依赖解析通过不等于 Mac 上安装和推理已实测。

依赖和模型体积较大，需预留数 GB 空间和稳定网络；第一次转写还会下载 FunASR 模型。该步骤不会读取或上传你的录音。

## 配置 API 与启动

```bash
bash configure-macos.command
.venv/bin/python macos/check.py
bash start-macos.command
```

配置向导中粘贴自己的 DeepSeek API Key。输入不会显示；可选 SnapAny Key 可以直接回车跳过。没有 API Key 也可以先验证录音与本地转写。

安装完成后也可以双击 `start-macos.command`。如果系统对下载的脚本有打开提示，按 macOS 正常提示处理，或继续从终端执行上面的命令。

启动声年后点击界面的开始按钮。macOS 首次询问麦克风权限时，请允许启动声年的“终端”访问麦克风。若之前拒绝，请到“系统设置 → 隐私与安全性 → 麦克风”打开权限，退出后重开终端及声年。从其他终端应用启动时，授权对象可能是该终端应用。

设备选择顺序：手动选择的麦克风 → DJI 主麦 → 配置的备用设备 → 系统默认输入设备。可以在声年的“麦克风”窗口中手动选择 Mac 内置麦或 USB 麦克风。

## 数据和模型

- 默认数据目录：`~/Library/Application Support/VoiceJournal/Data`。
- 本地配置文件：源码目录 `src/config.toml`；可以在文本编辑器中修改。
- API Key 文件：数据目录 `runtime/api-keys.json`，以本机明文 JSON 保存，权限 `0600`。不要发送给他人。
- API Key 读取顺序：当前进程环境变量优先，再读上述本地文件。更改配置后重启声年。
- `VOICE_JOURNAL_DATA_ROOT` 可指定自己的数据目录；`VOICE_JOURNAL_CONFIG` 可指定另一份 TOML 配置文件。
- FunASR 和声纹模型首次下载到相关库的本机缓存；当前未随包内置模型。
- Mac 默认 `device = "auto"` 会选择 CPU。MPS 速度和模型兼容性尚未测试，不作加速承诺。
- 不需要 Obsidian 即可使用；如果要连接，在 `[obsidian].vault` 中填自己的 Mac 仓库绝对路径。

录音、转写和历史存本机。调用 AI 总结时，相关文字才会发送到你配置的服务商；录音不经声年服务器。

## 可选登录自启

先完成手动启动和麦克风权限验收，再按需要执行：

```bash
.venv/bin/python macos/autostart.py install
```

它会在本机用户的 LaunchAgents 中注册一项登录任务，并立即通过终端打开声年。**只打开界面，不自动开始录音**；关闭窗口后不会被反复拉起。移动源码目录后需要重新设置。

取消：

```bash
.venv/bin/python macos/autostart.py uninstall
```

取消自启不会删除录音、历史、API 配置或已打开的窗口。

## 在 Mac 上验收

基础检查不会录音、下载模型或调用 AI：

```bash
.venv/bin/python macos/check.py
```

需要下载/加载本地 ASR 与声纹模型时：

```bash
.venv/bin/python macos/check.py --load-model
```

明确录制 10 秒测试音频（先停止声年中的常驻录音）：

```bash
.venv/bin/python macos/check.py --record-seconds 10
```

输出会给出本机测试 WAV 路径，保存在数据目录的 `runtime/macos-check`，不会加入正式语音日记。请用清晰讲话的 WAV 测试本地转写：

```bash
.venv/bin/python macos/check.py --transcribe "/你的测试音频.wav"
```

人工检查：

- 界面能打开，中文清晰；历史窗口能显示转写、打开音频和笔记。
- 常驻录音能生成切片；停止时当前语音段正常保存；再次启动正常。
- 拔插 USB/DJI 麦克风、切换内置麦后，声音恢复且不重复录制。
- 重复打开声年只提示已有窗口；异常退出后重开不会出现双重转写。
- 连续使用 30–60 分钟，记录转写耗时、内存与温度，检查是否积压。
- 屏幕熄灭时仍可录音；手动睡眠后唤醒能重新连接。防休眠只针对空闲睡眠，不能保证合盖或手动睡眠期间录音。
- 用户主动生成一次 AI 总结，核对结果；查看 API 状态时不应触发请求。
- 根据需要验证导入 M4A/MP3、公开链接、声纹及 Obsidian。平台抓取成功率不在本轮兼容性保证范围内。

运行自动化测试：

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

其中两项 POSIX 锁测试在 Windows 会跳过，需要在 Mac 上实际执行。

## 后续安装包

Mac 实机验收通过后，再制作独立 `.app` / `.dmg`，处理应用的麦克风权限描述、模型与运行时资源、签名和公证。现阶段不提供未经实测的成品安装包。

技术依据：[Qt macOS 支持](https://doc.qt.io/qtforpython-6/overviews/qtdoc-macos.html)、[sounddevice 安装说明](https://python-sounddevice.readthedocs.io/en/latest/installation.html)、[FunASR 设备配置](https://github.com/modelscope/FunASR/blob/main/funasr/auto/auto_model.py)、[Apple 麦克风权限描述](https://developer.apple.com/documentation/bundleresources/information-property-list/nsmicrophoneusagedescription)、[PyInstaller 各平台打包要求](https://pyinstaller.org/en/stable/usage.html)。
