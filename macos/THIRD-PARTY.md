# 随应用提供的第三方组件

声年源码采用 MIT 许可证。Python、Qt/PySide6、PyTorch、FunASR 等组件保留各自许可证；完整版本清单与依赖分发包中的 LICENSE/NOTICE 随应用放在 `Contents/Resources/licenses`。

本地模型来自以下公开 ModelScope 模型仓库，模型卡和模型文件随包保留：

- https://modelscope.cn/models/iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch
- https://modelscope.cn/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
- https://modelscope.cn/models/iic/punc_ct-transformer_cn-en-common-vocab471067-large
- https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common

录音测试仅使用模型仓库自带的公开示例音频。用户的真实录音、声纹、笔记和 API Key 不进入构建环境。

FFmpeg 以独立进程解码音频，通过 imageio-ffmpeg 提供的 macOS arm64 预编译文件分发；构建参数保存在 `licenses/dependencies/ffmpeg-build.txt`。FFmpeg 的许可证依实际构建配置而定，不等同于 imageio-ffmpeg Python 包的 BSD 许可证。

- FFmpeg 上游源代码与许可证：https://ffmpeg.org/download.html
- 二进制构建脚本和源码入口：https://github.com/imageio/imageio-ffmpeg/tree/main/binaries
- FFmpeg 许可证说明：https://ffmpeg.org/legal.html
- Qt/PySide6 源码：https://code.qt.io/cgit/pyside/pyside-setup.git/

Mac 安装包的发布记录应与该版本对应的源码一同保留。
