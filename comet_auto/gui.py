from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt5 import QtCore, QtWidgets

from .comet import CometController
from .config import AppConfig, load_config, save_config


class SetupDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configuración inicial")
        self.setModal(True)

        exe_label = QtWidgets.QLabel("Ruta a comet.exe:")
        self.exe_edit = QtWidgets.QLineEdit()
        browse = QtWidgets.QPushButton("Buscar…")
        browse.clicked.connect(self._browse)

        port_label = QtWidgets.QLabel("Puerto debug:")
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(9223)

        self.auto_launch = QtWidgets.QCheckBox("Auto-lanzar Comet")
        self.auto_launch.setChecked(True)
        self.restart = QtWidgets.QCheckBox("Reiniciar Comet si está abierto sin debug-port")
        self.restart.setChecked(True)

        form = QtWidgets.QGridLayout()
        form.addWidget(exe_label, 0, 0)
        form.addWidget(self.exe_edit, 0, 1)
        form.addWidget(browse, 0, 2)
        form.addWidget(port_label, 1, 0)
        form.addWidget(self.port_spin, 1, 1)
        form.addWidget(self.auto_launch, 2, 0, 1, 3)
        form.addWidget(self.restart, 3, 0, 1, 3)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._autofill()

    def _autofill(self) -> None:
        detected = CometController.detect_comet_exe()
        if detected:
            self.exe_edit.setText(detected)

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Seleccionar comet.exe", str(Path.home()), "EXE (*.exe)")
        if path:
            self.exe_edit.setText(path)

    def get_config(self) -> AppConfig:
        exe = self.exe_edit.text().strip()
        if not exe:
            raise ValueError("Ruta de comet.exe vacía")
        return AppConfig(
            comet_exe=exe,
            debug_port=int(self.port_spin.value()),
            auto_launch=bool(self.auto_launch.isChecked()),
            restart_if_no_debug_port=bool(self.restart.isChecked()),
        )


class AskWorker(QtCore.QThread):
    status_text = QtCore.pyqtSignal(str)
    response_ready = QtCore.pyqtSignal(str)
    error_text = QtCore.pyqtSignal(str)

    def __init__(self, comet: CometController, prompt: str, new_chat: bool) -> None:
        super().__init__()
        self._comet = comet
        self._prompt = prompt
        self._new_chat = new_chat

    def run(self) -> None:
        try:
            self.status_text.emit("Conectando a Comet…")
            self._comet.connect_best_tab()
            self.status_text.emit("Enviando prompt…")
            resp = self._comet.ask(self._prompt, new_chat=self._new_chat, timeout_s=120.0)
            self.response_ready.emit(resp)
            self.status_text.emit("Listo.")
        except Exception as e:
            self.error_text.emit(str(e))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Comet Auto")
        self.resize(900, 650)

        self.cfg = load_config()
        if self.cfg is None:
            self.cfg = self._first_time_setup()

        self.comet = CometController(self.cfg)
        self.worker: AskWorker | None = None

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QVBoxLayout(root)

        top = QtWidgets.QHBoxLayout()
        self.prompt_edit = QtWidgets.QLineEdit()
        self.prompt_edit.setPlaceholderText("Escribe tu prompt…")
        self.new_chat = QtWidgets.QCheckBox("New chat")
        self.send_btn = QtWidgets.QPushButton("Enviar")
        self.send_btn.clicked.connect(self._send)
        top.addWidget(self.prompt_edit, 1)
        top.addWidget(self.new_chat)
        top.addWidget(self.send_btn)
        layout.addLayout(top)

        self.status = QtWidgets.QLabel("Listo.")
        layout.addWidget(self.status)

        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)

        info = QtWidgets.QLabel(
            "Tip: si Perplexity pide login, abre la ventana de Comet y loguéate una vez."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

    def _first_time_setup(self) -> AppConfig:
        dlg = SetupDialog(self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            raise SystemExit(1)
        cfg = dlg.get_config()
        save_config(cfg)
        return cfg

    def _send(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        prompt = self.prompt_edit.text().strip()
        if not prompt:
            return
        self.send_btn.setEnabled(False)
        self.status.setText("Iniciando…")
        self.output.appendPlainText(f"> {prompt}\n")

        self.worker = AskWorker(self.comet, prompt, bool(self.new_chat.isChecked()))
        self.worker.status_text.connect(self.status.setText)
        self.worker.response_ready.connect(self._on_response)
        self.worker.error_text.connect(self._on_error)
        self.worker.finished.connect(lambda: self.send_btn.setEnabled(True))
        self.worker.start()

    def _on_response(self, text: str) -> None:
        cleaned = text.strip()
        self.output.appendPlainText(cleaned + "\n")
        print(cleaned, flush=True)
        print("===COMPLETED===", flush=True)
        self.output.appendPlainText("===COMPLETED===\n")

    def _on_error(self, text: str) -> None:
        self.status.setText("Error")
        print(f"[error] {text}", flush=True)
        QtWidgets.QMessageBox.critical(self, "Error", text)


def run_gui() -> int:
    app = QtWidgets.QApplication([])
    w = MainWindow()
    w.show()
    return app.exec_()
