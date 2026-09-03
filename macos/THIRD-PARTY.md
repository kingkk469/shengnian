# 随应用提供的第三方组件

声年源码采用 MIT 许可证。Python、Qt/PySide6、PyTorch、FunASR 等组件保留各自许可证；完整版本清单与依赖分发包中的 LICENSE/NOTICE 随应用放在 `Contents/Resources/licenses`。

本地模型来自以下公开 ModelScope 模型仓库，模型卡和模型文件随包保留：

- https://modelscope.cn/models/iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch
- https://modelscope.cn/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
- https://modelscope.cn/models/iic/punc_ct-transformer_cn-en-common-vocab471067-large
- https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common

录音测试仅使用模型仓库自带的公开示例音频。用户的真实录音、声纹、笔记和 API Key 不进入构建环境。

FFmpeg 9.0.1 以独立进程解码音频，从 FFmpeg 官方源码为 macOS arm64 编译，禁用 GPL、nonfree 及第三方自动检测。对应源码压缩包、构建脚本和 LGPL 许可证随应用保存在 `licenses/dependencies/ffmpeg`，可使用同样的源码和脚本重新编译。

- FFmpeg 上游源代码与许可证：https://ffmpeg.org/download.html
- 二进制构建脚本：声年源码中的 `macos/build_ffmpeg.py`
- FFmpeg 许可证说明：https://ffmpeg.org/legal.html
- Qt/PySide6 源码：https://code.qt.io/cgit/pyside/pyside-setup.git/

Mac 安装包的发布记录应与该版本对应的源码一同保留。
