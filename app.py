import asyncio
import logging
import os
import sys
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UNITREE_REPO = BASE_DIR.parent / "unitree_webrtc_connect"
UNITREE_REPO = Path(os.environ.get("UNITREE_WEBRTC_REPO", DEFAULT_UNITREE_REPO)).resolve()
RECORDINGS_DIR = Path(os.environ.get("UNITREE_RECORDINGS_DIR", BASE_DIR / "recordings")).resolve()

if UNITREE_REPO.exists():
    sys.path.insert(0, str(UNITREE_REPO))


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True)
class RecorderConfig:
    method: str
    duration: float
    filename: str
    ip: str = ""
    serial: str = ""
    username: str = ""
    password: str = ""
    aes_key: str = ""
    region: str = "global"
    device_type: str = "Go2"


def sanitize_wav_filename(value: str) -> str:
    value = secure_filename(value.strip())
    if not value:
        value = f"go2_audio_{datetime.now():%Y%m%d_%H%M%S}.wav"
    if not value.lower().endswith(".wav"):
        value += ".wav"
    return value


def parse_duration(value: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("録音時間は数値で入力してください。") from exc

    if not 0.5 <= duration <= 600:
        raise ValueError("録音時間は 0.5 秒から 600 秒の範囲で入力してください。")
    return duration


def make_config(form: Dict[str, Any]) -> RecorderConfig:
    method = str(form.get("method", "localsta")).strip().lower()
    if method not in {"localsta", "localap", "remote"}:
        raise ValueError("接続方式が正しくありません。")

    ip = str(form.get("ip", "")).strip()
    serial = str(form.get("serial", "")).strip()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", "")).strip()
    aes_key = str(form.get("aes_key", "")).strip()
    region = str(form.get("region", "global")).strip() or "global"

    if method == "localsta" and not ip and not serial:
        raise ValueError("Local STA では IP アドレスまたはシリアル番号を入力してください。")
    if method == "remote" and not (serial and username and password):
        raise ValueError("Remote ではシリアル番号、ユーザー名、パスワードを入力してください。")

    return RecorderConfig(
        method=method,
        duration=parse_duration(str(form.get("duration", "5"))),
        filename=sanitize_wav_filename(str(form.get("filename", ""))),
        ip=ip,
        serial=serial,
        username=username,
        password=password,
        aes_key=aes_key,
        region=region,
    )


def create_connection(config: RecorderConfig) -> Any:
    try:
        from unitree_webrtc_connect.webrtc_driver import UnitreeWebRTCConnection, WebRTCConnectionMethod
    except Exception as exc:  # noqa: BLE001 - allow the web UI to run without robot/WebRTC deps.
        logger.exception("Unitree WebRTC ライブラリを読み込めませんでした。")
        raise RuntimeError(
            "Unitree/WebRTC 系ライブラリを読み込めませんでした。"
            "この PC では Web UI は起動できますが、録音には実機用環境が必要です。"
        ) from exc

    method_map = {
        "localsta": WebRTCConnectionMethod.LocalSTA,
        "localap": WebRTCConnectionMethod.LocalAP,
        "remote": WebRTCConnectionMethod.Remote,
    }
    method = method_map[config.method]

    common = {
        "serialNumber": config.serial or None,
        "aes_128_key": config.aes_key or None,
        "device_type": config.device_type,
    }

    if method == WebRTCConnectionMethod.LocalSTA:
        return UnitreeWebRTCConnection(method, ip=config.ip or None, **common)
    if method == WebRTCConnectionMethod.Remote:
        return UnitreeWebRTCConnection(
            method,
            username=config.username,
            password=config.password,
            region=config.region,
            **common,
        )
    return UnitreeWebRTCConnection(method, **common)


def audio_frame_to_pcm(frame: Any, channels: int = CHANNELS) -> Any:
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001 - surface as a recording error, not a web startup error.
        logger.exception("numpy を読み込めませんでした。")
        raise RuntimeError("録音には numpy が必要です。実機用環境で依存関係をインストールしてください。") from exc

    data = np.asarray(frame.to_ndarray())
    if data.dtype != np.int16:
        if np.issubdtype(data.dtype, np.floating):
            data = np.clip(data, -1.0, 1.0) * 32767
        data = np.clip(data, -32768, 32767).astype(np.int16)

    if data.ndim == 2 and data.shape[0] == channels:
        data = data.T

    return np.ascontiguousarray(data.reshape(-1), dtype=np.int16)


def unique_recording_path(filename: str) -> Path:
    path = RECORDINGS_DIR / filename
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10_000):
        candidate = RECORDINGS_DIR / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("保存ファイル名を決定できませんでした。")


class RecordingJob:
    def __init__(self, config: RecorderConfig) -> None:
        self.config = config
        self.path = unique_recording_path(config.filename)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="unitree-recorder", daemon=True)
        self.status = "starting"
        self.error = ""
        self.frames_recorded = 0
        self.bytes_written = 0
        self.started_at = datetime.now()
        self.finished_at = None  # type: Optional[datetime]

    @property
    def is_active(self) -> bool:
        return self.status in {"starting", "connecting", "recording", "stopping"}

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._update(status="stopping")

    def _update(self, **values: Any) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            progress = min(self.frames_recorded / (self.config.duration * SAMPLE_RATE), 1.0)
            return {
                "active": self.is_active,
                "status": self.status,
                "error": self.error,
                "filename": self.path.name,
                "download_url": url_for("download_recording", filename=self.path.name)
                if self.path.exists()
                else "",
                "duration": self.config.duration,
                "frames_recorded": self.frames_recorded,
                "seconds_recorded": round(self.frames_recorded / SAMPLE_RATE, 2),
                "progress": round(progress, 4),
                "bytes_written": self.bytes_written,
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M:%S")
                if self.finished_at
                else "",
            }

    def _run(self) -> None:
        try:
            asyncio.run(self._record())
        except Exception as exc:  # noqa: BLE001 - surface hardware/network errors in the UI.
            logger.exception("Recording failed")
            self._update(status="error", error=str(exc), finished_at=datetime.now())

    async def _record(self) -> None:
        target_frames = int(self.config.duration * SAMPLE_RATE)
        conn = None  # type: Optional[Any]
        wav_file = None  # type: Optional[wave.Wave_write]
        done_accepting_frames = False

        try:
            self._update(status="connecting")
            conn = create_connection(self.config)
            await conn.connect()

            wav_file = wave.open(str(self.path), "wb")
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
            wav_file.setframerate(SAMPLE_RATE)

            async def recv_audio_stream(frame: Any) -> None:
                nonlocal done_accepting_frames
                if done_accepting_frames or self.stop_event.is_set() or wav_file is None:
                    return

                pcm = audio_frame_to_pcm(frame)
                with self.lock:
                    remaining_frames = target_frames - self.frames_recorded

                if remaining_frames <= 0:
                    self.stop_event.set()
                    return

                max_samples = remaining_frames * CHANNELS
                if len(pcm) > max_samples:
                    pcm = pcm[:max_samples]

                frames_in_chunk = len(pcm) // CHANNELS
                if frames_in_chunk <= 0:
                    return

                wav_file.writeframes(pcm.tobytes())
                with self.lock:
                    self.frames_recorded += frames_in_chunk
                    self.bytes_written += len(pcm) * SAMPLE_WIDTH_BYTES
                    if self.frames_recorded >= target_frames:
                        self.stop_event.set()

            conn.audio.switchAudioChannel(True)
            conn.audio.add_track_callback(recv_audio_stream)
            self._update(status="recording")

            while not self.stop_event.is_set():
                await asyncio.sleep(0.1)

            done_accepting_frames = True
            self._update(status="stopping")
        finally:
            done_accepting_frames = True
            if conn is not None:
                try:
                    conn.audio.switchAudioChannel(False)
                except Exception:
                    logger.debug("Could not switch audio channel off", exc_info=True)
                try:
                    await conn.disconnect()
                except Exception:
                    logger.debug("Could not disconnect cleanly", exc_info=True)
            if wav_file is not None:
                wav_file.close()

        final_status = "finished" if self.frames_recorded >= target_frames else "stopped"
        self._update(status=final_status, finished_at=datetime.now())


class RecordingManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_job = None  # type: Optional[RecordingJob]
        self.last_error = ""

    def start(self, config: RecorderConfig) -> RecordingJob:
        with self.lock:
            if self.current_job and self.current_job.is_active:
                raise RuntimeError("すでに録音中です。停止してから開始してください。")

            job = RecordingJob(config)
            self.current_job = job
            self.last_error = ""
            job.start()
            return job

    def stop(self) -> None:
        with self.lock:
            if self.current_job and self.current_job.is_active:
                self.current_job.stop()

    def status(self) -> Dict[str, Any]:
        with self.lock:
            if not self.current_job:
                return {"active": False, "status": "idle", "error": self.last_error}
            return self.current_job.snapshot()


manager = RecordingManager()


def list_recordings() -> List[Dict[str, Any]]:
    recordings = []
    for path in sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        recordings.append(
            {
                "name": path.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "download_url": url_for("download_recording", filename=path.name),
            }
        )
    return recordings


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        status=manager.status(),
        recordings=list_recordings(),
        default_ip=os.environ.get("UNITREE_GO2_IP", "192.168.8.181"),
        unitree_repo=str(UNITREE_REPO),
    )


@app.post("/record/start")
def start_recording():
    try:
        config = make_config(request.form)
        job = manager.start(config)
    except Exception as exc:  # noqa: BLE001 - return validation and busy-state errors to the UI.
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "error": str(exc)}), 400
        return redirect(url_for("index", error=str(exc)))

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "job": job.snapshot()})
    return redirect(url_for("index"))


@app.post("/record/stop")
def stop_recording():
    manager.stop()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "status": manager.status()})
    return redirect(url_for("index"))


@app.get("/api/status")
def api_status():
    return jsonify(manager.status())


@app.get("/recordings/<path:filename>")
def download_recording(filename: str):
    return send_from_directory(RECORDINGS_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
