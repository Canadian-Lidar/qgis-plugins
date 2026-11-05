"""Background work in QGIS tasks; messages reach the GUI through signals."""
import traceback

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ..core import engine, pipeline


class WorkTask(QgsTask):
    """Run fn(ctx) in the background. Signals: message(level, text),
    stage(text), done(ok, result_or_error)."""

    message = pyqtSignal(str, str)
    stage = pyqtSignal(str)
    done = pyqtSignal(bool, object)

    def __init__(self, title, fn):
        super().__init__(title, QgsTask.Flag.CanCancel)
        self.fn = fn
        self.result_value = None
        self.error = None
        self._last_stage = ""

    def _progress(self, f, text):
        self.setProgress(100.0 * f)
        if text and text != self._last_stage:
            self._last_stage = text
            self.stage.emit(text)

    def run(self):
        ctx = pipeline.Ctx(self._progress, lambda lvl, t: self.message.emit(lvl, t), self.isCanceled)
        try:
            self.result_value = self.fn(ctx)
            return True
        except engine.Cancelled:
            self.error = "Cancelled"
            return False
        except (pipeline.StepError, engine.EngineError) as e:
            self.error = str(e)
            return False
        except Exception as e:     # noqa: BLE001
            # download and CSRS-PPP failures are plain messages, not program errors
            from ..core import csrs, products
            if isinstance(e, (products.DownloadError, csrs.CsrsError)):
                self.error = str(e)
                return False
            self.error = "%s\n\n%s" % (e, traceback.format_exc())
            return False

    def finished(self, ok):
        self.done.emit(bool(ok), self.result_value if ok else (self.error or "Cancelled"))
