"""Local API configuration shared by the Mac app and source installer."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile


def save_key(path: Path, name: str, value: str) -> None:
    if name not in {"DEEPSEEK_API_KEY", "SNAPANY_API_KEY"}:
        raise ValueError("不支持的 API 类型")
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(payload, dict):
        raise ValueError("已有 API 配置格式异常，未覆盖原文件")
    payload[name] = value.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name_tmp = tempfile.mkstemp(prefix=".api-keys-", suffix=".json", dir=path.parent)
    temporary = Path(name_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def show_api_dialog(parent, root: Path):
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QFormLayout, QLineEdit, QDialogButtonBox, QMessageBox
    dialog = QDialog(parent)
    dialog.setWindowTitle("API 配置")
    dialog.resize(510, 250)
    layout = QVBoxLayout(dialog)
    tip = QLabel("录音与转写在本机运行。使用 AI 总结时，相关文字会直接发送到你配置的服务商。")
    tip.setWordWrap(True)
    layout.addWidget(tip)
    form = QFormLayout()
    fields = {}
    for name, label in (("DEEPSEEK_API_KEY", "DeepSeek API Key"), ("SNAPANY_API_KEY", "SnapAny（可选）")):
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText("已配置；留空保留" if os.environ.get(name) else "粘贴自己的 API Key")
        fields[name] = field
        form.addRow(label, field)
    layout.addLayout(form)
    hint = QLabel("密钥只保存在本机，不会显示在日志中。保存时不会调用 API。")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)
    def save():
        try:
            for name, field in fields.items():
                value = field.text().strip()
                if value:
                    save_key(root / "runtime/api-keys.json", name, value)
                    os.environ[name] = value
            dialog.accept()
        except (OSError, ValueError):
            QMessageBox.warning(dialog, "保存失败", "未能保存本机 API 配置，请检查数据目录权限或原配置文件。")
    buttons.accepted.connect(save)
    buttons.rejected.connect(dialog.reject)
    return dialog.exec()
