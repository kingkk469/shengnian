# Mac 录音中断修复候选（2026-09-18）

用户反馈：Mac 点击录音后，一说话就中断。尚未取得该设备的录音日志，不能把下述代码隐患认定为唯一根因。

## 已修改

- Mac 优先使用设备原生采样率，之后转换为现有的 16 kHz、20 ms 识别帧。
- 实时音频回调只复制音频到有上限的缓冲队列；语音检测、重采样、日志和 WAV 写入转到录音会话线程，避免文件操作阻塞采集线程。
- 正常退出或设备异常时先停止采集，再处理队列并保存当前有效片段，消除后台回调与主线程同时修改片段的问题。
- Mac 录音期间的设备选择只枚举设备，不反复探测多种采样率。
- 及时检测音频流停止、缓冲溢出和音频处理错误，交给现有重连流程；数据到达检查独立于暂停状态。

技术依据：[sounddevice 回调要求](https://python-sounddevice.readthedocs.io/en/latest/_modules/sounddevice.html)：实时回调不得进行文件系统访问或执行耗时不可预测的操作。

## 验证

Windows 本地执行 `python -m pytest -q`：86 passed，2 skipped。使用隔离测试目录和合成音频，未访问真实麦克风、日记或云端 AI。

新增 5 项测试覆盖原生采样率优先级、VAD 帧格式、运行期间无格式探测、采集线程不执行语音处理、流停止后保留已接收的完整语音片段。

## Mac 构建结果

用户授权上传后，代码已推送至独立分支 `codex/macos-app-recording-fix-20260918`，构建提交 `d0aa3833bc6df1f7478dae727c7d30417864f650`。

[Mac 构建 35324135370](https://github.com/kingkk469/shengnian/actions/runs/35324135370) 全部成功：88 项 Mac 回归通过；冻结应用实际通过本地中文转写、压缩音频导入、192 维声纹、Cocoa 主窗口和历史窗口检查；DMG 完整性、只读挂载和签名检查通过。

完整安装包产物 `shengnian-macos-arm64`：2,446,742,775 字节，产物 ID `10538744481`。ZIP SHA256：`3733890230ed798f5ff3d8ceecbfb583b1bd3c0c86f195f678244c2cfb0d7acd`。

验证报告保存在本机 `release/macos-recording-fix-20260918/evidence`。

## 尚未完成

- 本次产物为 Actions 构建包，尚未替换公开 Release 中的旧 beta.1。
- 未在真实 Mac 麦克风上复现或验证；需安装新构建后连续说话、停顿再说、暂停恢复及切换输入设备检查。
- 若仍中断，需查看本机 `~/Library/Application Support/VoiceJournal/Data/logs/recorder.log` 中对应时间的错误；无需提供日记正文或录音。

既有 beta.1 Release 和旧安装包不包含本次修改。
