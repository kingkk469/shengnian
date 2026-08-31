"""声年对 FunASR 的最小兼容垫片。

FunASR 1.3.1 在 ``funasr.utils.load_utils`` 中无条件导入 ``kaldiio``，
但本产品只接受普通音频文件，不支持 Kaldi ARK/SCP 输入。上游 kaldiio
发行包所附许可证仅允许评估且禁止再分发，因此商业构建不得包含其代码。

这个文件是声年自有代码，不包含或改编 kaldiio 的实现。
"""


def load_mat(*_args, **_kwargs):
    """明确拒绝产品未开放的 Kaldi ARK 输入。"""
    raise RuntimeError("声年不支持 Kaldi ARK/SCP 输入")


def __getattr__(name: str):
    raise AttributeError(
        f"声年的 kaldiio 兼容垫片未实现 {name!r}；"
        "声年仅支持 WAV 等普通音频文件"
    )
