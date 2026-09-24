# -*- coding: utf-8 -*-
"""茉莉运动 · 桌面版（PyQt6）

用法：
    python moli.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QFile, QIODevice, QUrl, QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication

from bridge import Bridge

ROOT = Path(__file__).resolve().parent
ENTRY = (ROOT / "static" / "index.html").as_uri() + "#app"

SETUP_JS = """
new QWebChannel(qt.webChannelTransport, function(channel){
  var api = channel.objects.api;
  window.__pending = {}; window.__cbid = 0;
  window.__resolve = function(id, v){ var f = window.__pending[id]; if (f) { delete window.__pending[id]; f(v); } };
  window.pywebview = { api: {} };
  %s.forEach(function(m){
    window.pywebview.api[m] = function(payload){
      return new Promise(function(resolve){
        var id = ++window.__cbid;
        window.__pending[id] = resolve;
        api.invoke(m, id, JSON.stringify(payload === undefined ? null : payload));
      });
    };
  });
  window.pywebview.openUrl = function(url){ api.open_url(String(url || '')); };
  window.pywebview.copyText = function(text){ api.copy_text(String(text || '')); };
  document.dispatchEvent(new Event('pywebviewready'));
});
"""


def exposed_methods(bridge: Bridge) -> list[str]:
    return [m for m in dir(bridge) if not m.startswith("_") and callable(getattr(bridge, m))]


def qwebchannel_source() -> str:
    for path in (":/qtwebchannel/qwebchannel.js", "qrc:/qtwebchannel/qwebchannel.js"):
        f = QFile(path)
        if f.open(QIODevice.OpenModeFlag.ReadOnly):
            try:
                return bytes(f.readAll().data()).decode("utf-8")
            finally:
                f.close()
    raise RuntimeError("未找到 qwebchannel.js")


class _Task(QRunnable):
    def __init__(self, api: "Api", name: str, call_id: int, payload):
        super().__init__()
        self.setAutoDelete(True)
        self.api, self.name, self.call_id, self.payload = api, name, call_id, payload

    def run(self):
        self.api.dispatch(self.name, self.call_id, self.payload)


class Api(QObject):
    js_ready = pyqtSignal(str)
    open_url_requested = pyqtSignal(str)
    copy_text_requested = pyqtSignal(str)

    def __init__(self, page: QWebEnginePage, bridge: Bridge):
        super().__init__()
        self._page = page
        self._bridge = bridge
        self.js_ready.connect(self._run_js)  # 跨线程 → 自动排队到 GUI 线程
        self.open_url_requested.connect(self._open_url)
        self.copy_text_requested.connect(self._copy_text)

    @pyqtSlot(str, int, str)
    def invoke(self, name: str, call_id: int, payload_json: str):
        try:
            payload = json.loads(payload_json) if payload_json else None
        except Exception:
            payload = None
        QThreadPool.globalInstance().start(_Task(self, name, call_id, payload))

    @pyqtSlot(str)
    def open_url(self, url: str):
        self.open_url_requested.emit(url)

    @pyqtSlot(str)
    def copy_text(self, text: str):
        self.copy_text_requested.emit(text)

    def _open_url(self, url: str):
        if url.startswith(("http://", "https://")):
            QDesktopServices.openUrl(QUrl(url))

    def _copy_text(self, text: str):
        QApplication.clipboard().setText(text)

    def dispatch(self, name: str, call_id: int, payload):
        try:
            fn = getattr(self._bridge, name, None)
            if fn is None or name.startswith("_"):
                result: dict = {"__error": f"不支持的调用: {name}"}
            else:
                result = fn(payload)
            code = f"window.__resolve({call_id}, {json.dumps(result, ensure_ascii=True, default=str)})"
        except Exception as e:  # noqa: BLE001 - 统一兜底，避免 JS 侧悬挂
            code = f"window.__resolve({call_id}, {json.dumps({'__error': str(e)})})"
        self.js_ready.emit(code)

    def _run_js(self, code: str):
        self._page.runJavaScript(code)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("茉莉运动")

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    profile = QWebEngineProfile("moli", app)
    profile.setPersistentStoragePath(str(data_dir / "storage"))
    profile.setCachePath(str(data_dir / "cache"))

    bridge = Bridge()
    view = QWebEngineView()
    page = QWebEnginePage(profile, view)
    view.setPage(page)

    settings = page.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)

    channel = QWebChannel()
    api = Api(page, bridge)  # registerObject 不保活 Python 对象，必须持有引用
    channel.registerObject("api", api)
    page.setWebChannel(channel)

    methods = json.dumps(exposed_methods(bridge))

    def on_loaded(ok: bool):
        if not ok:
            return
        page.runJavaScript(qwebchannel_source(), lambda _: page.runJavaScript(SETUP_JS % methods))

    page.loadFinished.connect(on_loaded)

    view.setWindowTitle("茉莉运动")
    view.setFixedSize(375, 760)
    view.load(QUrl(ENTRY))
    view.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
