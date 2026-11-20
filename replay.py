"""Replay Buffer: постоянно пишет экран + звук в кольцевой буфер, по хоткею сохраняет последние N секунд."""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import winreg
import winsound
from collections import deque
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # иначе OpenBLAS резервирует буферы под каждое ядро (~500 МБ)
import numpy as np
import pyaudiowpatch as pa
import pystray
from PIL import Image, ImageDraw

APP_NAME = "Replay Buffer"
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FFMPEG = str(RES_DIR / "bin" / "ffmpeg.exe")
DATA_DIR = Path(os.environ["APPDATA"]) / "ReplayBuffer"
CONFIG_PATH = DATA_DIR / "config.json"
BUF_DIR = Path(os.environ.get("TEMP", str(DATA_DIR))) / "ReplayBuffer"
NO_WINDOW = 0x08000000

DEFAULT_CONFIG = {
    "duration_sec": 60,
    "hotkey": "alt+f10",
    "output_dir": "%USERPROFILE%\\Videos\\Replays",
    "monitor": 0,
    "fps": 60,
    "resolution": "original",    # original | 1440p | 1080p | 720p (уменьшение идёт на CPU)
    "quality": "medium",         # low | medium | high
    "codec": "h264",             # h264 | hevc
    "system_audio": True,
    "system_device": "",         # "" = устройство вывода по умолчанию
    "system_volume": 1.0,
    "mic": True,
    "mic_device": "",            # "" = микрофон по умолчанию
    "mic_volume": 1.0,
    "audio_offset_ms": 0,
    "autostart": False,
    "save_sound": "chime",       # chime | shutter | pop | coin | none | путь к .wav
    "sound_volume": 0.6,
    "separate_tracks": True,     # в клипе: дорожка «всё» + отдельно комп / Discord / микрофон
    "discord_track": True,       # Discord отдельной дорожкой (и вычитается из звука компа)
    "discord_volume": 1.0,
    "autoclip": True,            # автоклипы в CS2 через Game State Integration
    "autoclip_kills": 3,         # сколько киллов за раунд нужно для автоклипа
}
RESOLUTIONS = {"original": None, "1440p": 1440, "1080p": 1080, "720p": 720}
# (cq, maxrate в Мбит/с для 1080p60)
QUALITY = {"low": (31, 4), "medium": (27, 8), "high": (23, 16)}
SEGMENT_SEC = 1
PREVIEW_DIR = DATA_DIR / "previews"
PROCCAP = RES_DIR / "bin" / "proccap" / "proccap.exe"
TRACK_DIR = DATA_DIR / "tracks"
PREVIEW_LOCK = threading.Lock()
SR, CH = 48000, 2
PA_LOCK = threading.Lock()  # PortAudio не потокобезопасен: init/terminate/перечисление только под замком

log = logging.getLogger("replay")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except Exception as e:
            log.error("config.json битый, беру дефолт: %s", e)
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def output_dir(cfg):
    p = Path(os.path.expandvars(cfg["output_dir"]))
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- sounds

SOUND_RATE = 44100


def _tone(freq, dur, decay, harmonics=((1, 1.0),)):
    t = np.arange(int(SOUND_RATE * dur)) / SOUND_RATE
    wave = sum(a * np.sin(2 * np.pi * freq * k * t) for k, a in harmonics)
    env = np.exp(-t * decay) * np.minimum(1, t / 0.004)  # мягкая атака без щелчка
    return wave * env


def _at(total, part, start):
    i = int(start * SOUND_RATE)
    total[i:i + len(part)] += part[:len(total) - i]


def _synth(name):
    bell = ((1, 1.0), (2, 0.35), (3, 0.12))
    if name == "chime":  # два звонких колокольчика вверх
        out = np.zeros(int(SOUND_RATE * 0.7))
        _at(out, _tone(1318.5, 0.6, 7, bell), 0)
        _at(out, _tone(1975.5, 0.6, 6, bell), 0.09)
    elif name == "shutter":  # затвор камеры: два коротких щелчка шума
        rng = np.random.default_rng(7)
        out = np.zeros(int(SOUND_RATE * 0.22))
        for start, length, amp in ((0, 0.035, 1.0), (0.075, 0.05, 0.8)):
            n = int(SOUND_RATE * length)
            noise = np.convolve(rng.standard_normal(n), np.ones(6) / 6, "same")
            _at(out, noise * np.exp(-np.arange(n) / n * 6) * amp, start)
    elif name == "pop":  # мягкий «пузырь»: короткий тон со скольжением вверх
        t = np.arange(int(SOUND_RATE * 0.16)) / SOUND_RATE
        f = 520 + 900 * (t / t[-1]) ** 0.6
        out = np.sin(2 * np.pi * np.cumsum(f) / SOUND_RATE) * np.exp(-t * 22) * np.minimum(1, t / 0.003)
    elif name == "coin":  # монетка из аркад
        out = np.zeros(int(SOUND_RATE * 0.45))
        sq = lambda f, d: np.sign(np.sin(2 * np.pi * f * np.arange(int(SOUND_RATE * d)) / SOUND_RATE)) * 0.35
        _at(out, sq(988, 0.07), 0)
        part = sq(1319, 0.38) * np.exp(-np.arange(int(SOUND_RATE * 0.38)) / SOUND_RATE * 9)
        _at(out, part, 0.07)
    elif name == "error":  # два низких тона вниз
        out = np.zeros(int(SOUND_RATE * 0.5))
        _at(out, _tone(440, 0.22, 10, bell), 0)
        _at(out, _tone(311, 0.3, 9, bell), 0.16)
    else:
        return None
    return out / (np.abs(out).max() + 1e-9)


SOUNDS = ("chime", "shutter", "pop", "coin")


def sound_file(name, volume):
    """WAV встроенного звука с уже запечённой громкостью (winsound не умеет громкость)."""
    import wave
    vol = max(0.0, min(1.0, volume))
    path = DATA_DIR / "sounds" / f"{name}_{int(vol * 100)}.wav"
    if not path.exists():
        data = _synth(name)
        if data is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SOUND_RATE)
            w.writeframes((data * vol * 0.9 * 32767).astype("<i2").tobytes())
    return path


def play_sound(name, volume):
    if not name or name == "none":
        return
    try:
        path = Path(name) if name.lower().endswith(".wav") else sound_file(name, volume)
        if path and path.exists():
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        log.error("звук %s: %s", name, e)


def set_autostart(on):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    with key:
        if on:
            if FROZEN:
                cmd = f'"{sys.executable}" --hidden'
            else:
                cmd = f'"{Path(sys.executable).with_name("pythonw.exe")}" "{Path(__file__).resolve()}" --hidden'
            winreg.SetValueEx(key, "ReplayBuffer", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "ReplayBuffer")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- audio

def list_devices():
    """(loopback-устройства, микрофоны) — списки имён для настроек."""
    with PA_LOCK:
        p = pa.PyAudio()
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)["index"]
            outs, mics = [], []
            for d in devices(p):
                if d["hostApi"] != wasapi or d["maxInputChannels"] <= 0:
                    continue
                if d.get("isLoopbackDevice"):
                    outs.append(d["name"].replace(" [Loopback]", ""))
                else:
                    mics.append(d["name"])
            return outs, mics
        finally:
            p.terminate()


def devices(p):
    for i in range(p.get_device_count()):
        try:
            yield p.get_device_info_by_index(i)
        except OSError:  # отвалившиеся/виртуальные устройства
            pass


class Buffered:
    """Очередь 48 кГц стерео float32 между потоком захвата и микшером."""

    def __init__(self, volume):
        self.volume = volume
        self.buf = deque()
        self.size = 0
        self.level = 0.0  # пик последнего блока, для индикаторов
        self.lock = threading.Lock()

    def push(self, a):
        a = a.astype(np.float32) * self.volume
        self.level = float(np.abs(a).max()) if len(a) else 0.0
        with self.lock:
            self.buf.append(a)
            self.size += len(a)

    def take(self, n):
        out = np.zeros((n, 2), np.float32)
        with self.lock:
            while self.size > SR * 0.15 and len(self.buf) > 1:  # не даём задержке накапливаться
                self.size -= len(self.buf.popleft())
            got = 0
            while got < n and self.buf:
                a = self.buf[0]
                k = min(n - got, len(a))
                out[got:got + k] = a[:k]
                got += k
                if k == len(a):
                    self.buf.popleft()
                else:
                    self.buf[0] = a[k:]
                self.size -= k
        return out


def discord_pid():
    """PID главного процесса Discord (у остальных Discord.exe он родитель) или None."""
    import psutil
    procs = {p.pid: p for p in psutil.process_iter(["name", "ppid"]) if (p.info["name"] or "").lower() == "discord.exe"}
    roots = [pid for pid, p in procs.items() if p.info["ppid"] not in procs]
    return roots[0] if roots else None


class ProcSource(Buffered):
    """Звук процесса Discord через proccap.exe (WASAPI process loopback):
    include — только Discord, exclude — весь звук системы, кроме Discord (дорожка «комп»).
    Если Discord запустили или перезапустили — сам переподключается."""

    def __init__(self, mode, volume, name):
        super().__init__(volume)
        self.mode, self.name = mode, name
        self.proc = None
        self.pid = None
        self.stop_evt = threading.Event()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _target(self):
        pid = discord_pid()
        if pid is None and self.mode == "exclude":
            pid = os.getpid()  # Discord не запущен — исключаем сами себя, то есть берём весь звук
        return pid

    def _supervise(self):
        while not self.stop_evt.is_set():
            pid = self._target()
            if pid != self.pid or (self.proc and self.proc.poll() is not None):
                self._kill()
                self.pid = pid
                if pid:
                    self.proc = subprocess.Popen([str(PROCCAP), str(pid), self.mode], stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                    threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
                    log.info("audio source: %s (proccap %s pid %s)", self.name, self.mode, pid)
            self.stop_evt.wait(3)

    def _read(self, proc):
        chunk = SR // 100 * 8  # 10 мс float32 стерео
        while True:
            b = proc.stdout.read(chunk)
            if not b:
                return
            n = len(b) // 8 * 8
            self.push(np.frombuffer(b[:n], np.float32).reshape(-1, 2))

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None

    def close(self):
        self.stop_evt.set()
        self._kill()


class Source(Buffered):
    """Аудиоустройство через PortAudio (loopback вывода или микрофон), приводится к 48 кГц стерео."""

    def __init__(self, p, dev, volume):
        super().__init__(volume)
        self.rate = int(dev["defaultSampleRate"])
        self.ch = max(1, int(dev["maxInputChannels"]))
        self.stream = p.open(format=pa.paFloat32, channels=self.ch, rate=self.rate, input=True,
                             input_device_index=dev["index"], frames_per_buffer=self.rate // 100,
                             stream_callback=self._cb)
        log.info("audio source: %s (%d Hz, %d ch)", dev["name"], self.rate, self.ch)

    def _cb(self, data, frames, info, status):
        a = np.frombuffer(data, dtype=np.float32).reshape(-1, self.ch)
        a = np.repeat(a, 2, axis=1) if self.ch == 1 else a[:, :2]
        if self.rate != SR:
            n = int(round(len(a) * SR / self.rate))
            x = np.linspace(0, len(a) - 1, n)
            a = np.stack([np.interp(x, np.arange(len(a)), a[:, c]) for c in range(2)], axis=1)
        self.push(a)
        return None, pa.paContinue

    def close(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


class Mixer:
    """Сводит источники и пишет PCM в ffmpeg строго по реальному времени
    (WASAPI loopback молчит в тишине, поэтому дыры добиваем нулями)."""

    def __init__(self, cfg):
        with PA_LOCK:
            self._open(cfg)
        self.stop_evt = threading.Event()
        self.thread = None
        self.t0_wall = None

    def _open(self, cfg):
        self.p = pa.PyAudio()
        self.system = self.mic = self.discord = None
        separate = cfg["separate_tracks"]
        # отдельный Discord: его слышно своей дорожкой, а из «компа» он вычитается (process loopback exclude)
        use_discord = separate and cfg["discord_track"] and PROCCAP.exists()
        if cfg["system_audio"]:
            try:
                if use_discord:
                    self.system = ProcSource("exclude", cfg["system_volume"], "комп без Discord")
                else:
                    self.system = Source(self.p, self._find_loopback(cfg["system_device"]), cfg["system_volume"])
            except Exception as e:
                log.error("не смог открыть звук компа: %s", e)
        if use_discord:
            self.discord = ProcSource("include", cfg["discord_volume"], "Discord")
        if cfg["mic"]:
            try:
                self.mic = Source(self.p, self._find_mic(cfg["mic_device"]), cfg["mic_volume"])
            except Exception as e:
                log.error("не смог открыть микрофон: %s", e)
        named = [(t, s) for t, s in (("Комп", self.system), ("Discord", self.discord), ("Микрофон", self.mic)) if s]
        self.sources = [s for _, s in named]
        # дорожка 0 — общий микс (её играют все плееры и мессенджеры), дальше — по источнику
        self.split = separate and len(named) > 1
        self.titles = ["Всё"] + [t for t, _ in named] if self.split else ["Всё"]
        self.channels = 2 * len(self.titles)

    def _find_loopback(self, name):
        if name:
            for d in devices(self.p):
                if d.get("isLoopbackDevice") and d["name"].replace(" [Loopback]", "") == name:
                    return d
            log.warning("устройство вывода '%s' не найдено, беру по умолчанию", name)
        return self.p.get_default_wasapi_loopback()

    def _find_mic(self, name):
        wasapi = self.p.get_host_api_info_by_type(pa.paWASAPI)
        if name:
            for d in devices(self.p):
                if d["hostApi"] == wasapi["index"] and not d.get("isLoopbackDevice") and d["name"] == name and d["maxInputChannels"] > 0:
                    return d
            log.warning("микрофон '%s' не найден, беру по умолчанию", name)
        return self.p.get_device_info_by_index(wasapi["defaultInputDevice"])

    def start(self, pipe):
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, args=(pipe,), daemon=True)
        self.thread.start()

    def _run(self, pipe):
        t0 = time.perf_counter()
        self.t0_wall = time.time()
        written = 0
        while not self.stop_evt.is_set():
            need = int((time.perf_counter() - t0) * SR) - written
            if need > 0:
                parts = [s.take(need) for s in self.sources]
                mix = np.sum(parts, axis=0) if parts else np.zeros((need, 2), np.float32)
                frame = np.concatenate([mix] + parts, axis=1) if self.split else mix
                try:
                    pipe.write((np.clip(frame, -1, 1) * 32767).astype("<i2").tobytes())
                except Exception:
                    return
                written += need
            time.sleep(0.01)

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=2)

    def close(self):
        self.stop()
        with PA_LOCK:
            for s in self.sources:
                s.close()
            self.p.terminate()


# ---------------------------------------------------------------- recorder

class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.mixer = None
        self.session_dir = None
        self.stop_evt = threading.Event()
        self.stderr_tail = deque(maxlen=30)
        self.encoders = {"h264": ["h264_nvenc", "h264_amf", "libx264"],
                         "hevc": ["hevc_nvenc", "hevc_amf", "libx264"]}[cfg["codec"]]
        self.mode = 0
        self.titles = []
        self.last_span = None
        self.started_at = None
        self.thread = None
        self.cleaner = None

    def _cmd(self, seg_dir, titles):
        has_audio = bool(titles)
        c = self.cfg
        enc = self.encoders[self.mode]
        fps = int(c["fps"])
        height = RESOLUTIONS.get(c["resolution"])
        cq, mbit = QUALITY.get(c["quality"], QUALITY["medium"])
        mbit *= ((height or 1440) / 1080) * (fps / 60) ** 0.5
        if enc.startswith("hevc"):
            cq, mbit = cq + 1, mbit * 0.65
        br = f"{mbit:.1f}M"

        zero_copy = height is None and enc.endswith("nvenc")  # кадры прямо из видеопамяти в NVENC
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
               f"ddagrab=output_idx={c['monitor']}:framerate={fps}:draw_mouse=1"]
        if has_audio:
            if c["audio_offset_ms"]:
                cmd += ["-itsoffset", str(c["audio_offset_ms"] / 1000)]
            cmd += ["-f", "s16le", "-ar", str(SR), "-ac", str(2 * len(titles)), "-i", "pipe:0", "-map", "0:v"]
            if len(titles) == 1:
                cmd += ["-map", "1:a"]
            else:  # одна многоканальная труба → по стерео-дорожке на каждую пару каналов
                n = len(titles)
                graph = f"[1:a]asplit={n}" + "".join(f"[s{i}]" for i in range(n)) + ";" + ";".join(
                    f"[s{i}]pan=stereo|c0=c{2 * i}|c1=c{2 * i + 1}[a{i}]" for i in range(n))
                cmd += ["-filter_complex", graph]
                for i in range(n):
                    cmd += ["-map", f"[a{i}]"]
        if not zero_copy:
            size = f"-2:'min(ih,{height})':flags=bilinear:" if height else ""
            cmd += ["-vf", f"hwdownload,format=bgra,scale={size}out_color_matrix=bt709:out_range=tv,format=yuv420p",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        if enc.endswith("nvenc"):
            # p2 + небольшой пул поверхностей: ~10% NVENC и меньше памяти, качество держит cq
            cmd += ["-c:v", enc, "-preset", "p2", "-surfaces", "8", "-delay", "0",
                    "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                    "-maxrate", br, "-bufsize", br, "-spatial-aq", "1",
                    "-forced-idr", "1"]  # без этого NVENC игнорирует принудительные ключевые кадры
        elif enc.endswith("amf"):
            cmd += ["-c:v", enc, "-quality", "balanced", "-rc", "qvbr", "-qvbr_quality_level", str(cq),
                    "-maxrate", br, "-b:v", br]
        else:
            cmd += ["-c:v", enc, "-preset", "veryfast", "-crf", str(cq - 4), "-maxrate", br, "-bufsize", br]
        # ключевой кадр ровно раз в сегмент, иначе сегменты выходят по 4+ сек
        cmd += ["-g", str(fps * SEGMENT_SEC), "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SEC})"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-b:a:0", "160k"]
        cmd += ["-f", "segment", "-segment_time", str(SEGMENT_SEC), "-segment_format", "mpegts",
                str(seg_dir / "seg_%08d.ts")]
        return cmd

    def start(self):
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.cleaner = threading.Thread(target=self._cleanup_loop, daemon=True)
        self.cleaner.start()

    def _loop(self):
        while not self.stop_evt.is_set():
            self.session_dir = BUF_DIR / datetime.now().strftime("s%Y%m%d_%H%M%S_%f")
            self.session_dir.mkdir(parents=True, exist_ok=True)
            self.started_at = time.time()
            self.mixer = Mixer(self.cfg)
            self.titles = self.mixer.titles if self.mixer.sources else []
            cmd = self._cmd(self.session_dir, self.titles)
            log.info("ffmpeg: %s", subprocess.list2cmdline(cmd))
            started = time.time()
            self.stderr_tail.clear()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
            if self.mixer.sources:
                self.mixer.start(self.proc.stdin)
            self._wait_for_capture()
            self.mixer.close()
            if self.stop_evt.is_set():
                break
            log.error("ffmpeg упал (код %s): %s", self.proc.returncode, " | ".join(self.stderr_tail))
            tail = " ".join(self.stderr_tail).lower()
            encoder_broken = any(k in tail for k in ("opening encoder", "nvenc", "amf", "cannot load", "no capable devices"))
            if encoder_broken and time.time() - started < 8 and self.mode < len(self.encoders) - 1:
                self.mode += 1
                log.info("пробую другой энкодер: %s", self.encoders[self.mode])
            self.stop_evt.wait(2)

    def _wait_for_capture(self):
        # Размер последнего файла растёт и без видео. Признак живого захвата —
        # смена сегмента, которую FFmpeg выполняет по ключевому видеокадру.
        latest = None
        deadline = time.monotonic() + 15  # дайте устройствам время на запуск
        while self.proc.poll() is None and not self.stop_evt.wait(1):
            segments = self.segments()
            if segments and segments[-1] != latest:
                latest = segments[-1]
                deadline = time.monotonic() + 8 * SEGMENT_SEC
            elif time.monotonic() >= deadline:
                log.error("Нет новых видеосегментов: перезапускаю захват (%s)", latest)
                self.proc.kill()
                break
        self.proc.wait()

    def _read_stderr(self, proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line:
                self.stderr_tail.append(line)

    def origin(self):
        """Реальное время, соответствующее нулю потока. Сегмент N покрывает [origin + N, origin + N + 1).
        Время изменения файлов для этого не годится: на старте ffmpeg выплёвывает первые сегменты пачкой."""
        m = self.mixer
        return m.t0_wall if m and m.t0_wall else self.started_at

    @staticmethod
    def seg_index(f):
        return int(f.stem.split("_")[1])

    def segments(self):
        if not self.session_dir or not self.session_dir.exists():
            return []
        return sorted(self.session_dir.glob("seg_*.ts"))

    def _cleanup_loop(self):
        while not self.stop_evt.is_set():
            # чистим по времени, а не по числу файлов: длина сегмента не гарантирована
            keep = max(self.cfg["duration_sec"], 180 if self.cfg.get("autoclip") else 0)
            cutoff = time.time() - keep - 15
            for f in self.segments()[:-2]:
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
            for d in BUF_DIR.glob("s*"):  # хвосты прошлых сессий
                if d != self.session_dir:
                    shutil.rmtree(d, ignore_errors=True)
            self.stop_evt.wait(2)

    def save_clip(self, name_hint):
        dur = self.cfg["duration_sec"]
        pressed = time.time()
        since = pressed - dur
        session = self.session_dir
        before = self.segments()
        if not before:
            raise RuntimeError("буфер пуст — запись ещё не началась")
        deadline = time.monotonic() + SEGMENT_SEC + 1
        while time.monotonic() < deadline:
            now = self.segments()
            if self.session_dir != session or self.stop_evt.is_set():
                raise RuntimeError("захват перезапускается — попробуйте ещё раз")
            if now and now[-1] != before[-1]:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("нет новых видеокадров — клип не сохранён")

        # Последний файл ещё открыт. Берём только закрытые сегменты этой сессии.
        segs = [f for f in now[:-1] if f.stat().st_mtime >= since]
        if not segs:
            raise RuntimeError("в буфере нет свежего видео")
        out = output_dir(self.cfg) / f"{name_hint}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
        stamp = time.time_ns()
        tmp = BUF_DIR / f"clip_{stamp}.ts"
        pending = out.with_name(f".clip_{stamp}.part")
        try:
            with tmp.open("wb") as w:
                for s in segs:
                    with s.open("rb") as r:
                        shutil.copyfileobj(r, w)
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-abort_on", "empty_output_stream",
                   "-sseof", f"-{dur}", "-i", str(tmp), "-t", str(dur),
                   "-map", "0:v:0", "-map", "0:a?", "-c", "copy", *self._track_meta(),
                   "-movflags", "+faststart", "-f", "mp4", str(pending)]
            result = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", "replace")[-500:])
            pending.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
        end = self.origin() + (self.seg_index(segs[-1]) + 1) * SEGMENT_SEC
        self.last_span = (max(end - dur, self.origin() + self.seg_index(segs[0]) * SEGMENT_SEC), end)
        return out

    def _track_meta(self):
        """Подписи дорожек; «Всё» играет по умолчанию. В mp4 имя живёт в handler_name (его показывают плееры)."""
        meta = []
        for i, t in enumerate(self.titles):
            meta += [f"-metadata:s:a:{i}", f"title={t}", f"-metadata:s:a:{i}", f"handler_name={t}",
                     f"-disposition:a:{i}", "default" if i == 0 else "0"]
        return meta

    def save_highlight(self, name_hint, windows, kills):
        """Нарезка вокруг киллов, как на Leetify: берём только секундные сегменты буфера, попавшие в окна,
        и склеиваем без пережатия. Возвращает (файл, метки киллов в секундах от начала клипа)."""
        origin = self.origin()
        need_idx = int((max(e for _, e in windows) - origin) // SEGMENT_SEC)
        deadline = time.time() + 5
        while time.time() < deadline:  # ждём, пока закроется сегмент с концом последнего окна
            segs = self.segments()
            if segs and self.seg_index(segs[-1]) > need_idx:
                break
            time.sleep(0.1)
        runs = []  # отрезки из подряд идущих сегментов
        for f in self.segments()[:-1]:
            n = self.seg_index(f)
            start = origin + n * SEGMENT_SEC
            end = start + SEGMENT_SEC
            if not any(start < we and end > ws for ws, we in windows):
                continue
            if runs and n == runs[-1]["last"] + 1:
                runs[-1].update(last=n, end=end)
                runs[-1]["files"].append(f)
            else:
                runs.append({"files": [f], "start": start, "end": end, "last": n})
        if not runs:
            raise RuntimeError("в буфере уже нет этих киллов")
        out = output_dir(self.cfg) / f"{name_hint}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4"
        stamp = time.time_ns()
        parts = []
        for i, r in enumerate(runs):  # внутри отрезка время непрерывное — склеиваем байтами
            part = BUF_DIR / f"hl_{stamp}_{i}.ts"
            with open(part, "wb") as w:
                for f in r["files"]:
                    try:
                        with open(f, "rb") as rd:
                            shutil.copyfileobj(rd, w)
                    except OSError:
                        pass
            parts.append(part)
        # отрезки между собой — concat-демуксером: он сдвигает время, чтобы вырезанные паузы не стали дырами
        lst = BUF_DIR / f"hl_{stamp}.txt"
        lst.write_text("".join(f"file '{x.as_posix()}'\n" for x in parts), encoding="utf-8")
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(lst), "-map", "0", "-c", "copy", *self._track_meta(),
                            "-movflags", "+faststart", str(out)], capture_output=True, creationflags=NO_WINDOW)
        for x in parts + [lst]:
            x.unlink(missing_ok=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(r.stderr.decode("utf-8", "replace")[-500:])
        marks, offset = [], 0.0
        for run in runs:
            marks += [round(offset + k - run["start"], 2) for k in kills if run["start"] <= k <= run["end"]]
            offset += run["end"] - run["start"]
        return out, marks

    def stop(self):
        self.stop_evt.set()
        if self.mixer:
            self.mixer.stop()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for t in (self.thread, self.cleaner):
            if t:
                t.join(timeout=5)


# ---------------------------------------------------------------- hotkey

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
NAMED_KEYS = {"pause": 0x13, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
              "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91, "space": 0x20,
              **{f"num{i}": 0x60 + i for i in range(10)}}


def parse_hotkey(s):
    mods, vk = MOD_NOREPEAT, None
    for part in s.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
    if vk is None:
        raise ValueError(f"не понял хоткей: {s}")
    return mods, vk


class Hotkey:
    def __init__(self, combo, callback):
        self.combo, self.callback = combo, callback
        self.tid = None
        self.ok = threading.Event()
        self.error = None
        self.stopped = False
        threading.Thread(target=self._run, daemon=True).start()
        self.ok.wait(2)

    def _run(self):
        self.tid = kernel32.GetCurrentThreadId()
        try:
            mods, vk = parse_hotkey(self.combo)
            if not user32.RegisterHotKey(None, 1, mods, vk):
                raise RuntimeError(f"хоткей {self.combo.upper()} уже занят другой программой")
        except Exception as e:
            self.error = str(e)
            self.ok.set()
            return
        self.ok.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                threading.Thread(target=self.callback, daemon=True).start()
        user32.UnregisterHotKey(None, 1)

    def stop(self):
        self.stopped = True
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


def foreground_app_name():
    """Имя exe активного окна — чтобы клип назывался по игре."""
    try:
        hwnd = user32.GetForegroundWindow()
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        name = Path(buf.value).stem
        if name.lower() in ("", "explorer", "searchapp", "shellexperiencehost", "replaybuffer"):
            return "Desktop"
        return "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "Replay"
    except Exception:
        return "Replay"


# ---------------------------------------------------------------- UI (pywebview)

def monitor_list():
    """Мониторы в порядке, в котором их нумерует Desktop Duplication (основной первый)."""
    mons = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    def cb(h, hdc, rect, lp):
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(h, ctypes.byref(mi))
        r = mi.rcMonitor
        mons.append({"w": r.right - r.left, "h": r.bottom - r.top, "primary": bool(mi.dwFlags & 1)})
        return 1

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    mons.sort(key=lambda m: not m["primary"])
    return [f"{m['w']}×{m['h']}" + (" · основной" if m["primary"] else "") for m in mons]


def clip_codec(clip):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(clip)], capture_output=True, creationflags=NO_WINDOW)
    # без консоли ffmpeg печатает UTF-8, с консолью — в её ANSI-кодировке: пробуем строго UTF-8, иначе ANSI
    try:
        return r.stderr.decode("utf-8")
    except UnicodeDecodeError:
        return r.stderr.decode("mbcs", "replace")


def clip_meta(clip):
    f = DATA_DIR / "meta" / (clip.stem + ".json")
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def clip_tracks(clip, info):
    """Отдельные аудиодорожки клипа (кроме нулевой — общего микса) в кэш .m4a для плеера, без пережатия."""
    import re
    names = []
    for block in re.split(r"\n(?=\s*Stream #)", info):
        if re.search(r"Stream #0:\d+.*: Audio:", block):
            m = re.search(r"(?:handler_name|title)\s*:\s*(.+)", block)
            names.append(m.group(1).strip() if m and m.group(1).strip() != "SoundHandler" else "")
    if len(names) < 2:
        return []
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    outs = [TRACK_DIR / f"{clip.stem}__{i}.m4a" for i in range(1, len(names))]
    if not all(o.exists() and o.stat().st_mtime >= clip.stat().st_mtime for o in outs):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip)]
        for i, o in enumerate(outs, 1):
            cmd += ["-map", f"0:a:{i}", "-c", "copy", "-vn", str(o)]
        subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
        # дорожки удалённых клипов и всё старше 30 клипов — в мусор
        alive = {f.stem for f in clip.parent.glob("*.mp4")}
        stems = sorted({f.name.rsplit("__", 1)[0] for f in TRACK_DIR.glob("*.m4a")},
                       key=lambda st: max(f.stat().st_mtime for f in TRACK_DIR.glob(f"{st}__*.m4a")), reverse=True)
        for k, st in enumerate(stems):
            if st not in alive or k >= 30:
                for f in TRACK_DIR.glob(f"{st}__*.m4a"):
                    f.unlink(missing_ok=True)
    return [(names[i] or f"Дорожка {i + 1}", o) for i, o in enumerate(outs, 1) if o.exists()]


def preview_for(clip, build=True):
    """WebView2 без расширения HEVC не играет H.265 — для встроенного плеера делаем H.264-копию на видеокарте.
    Сам клип не трогаем; копии лежат в кэше и чистятся вместе с клипами."""
    out = PREVIEW_DIR / (clip.stem + ".mp4")
    if out.exists() and out.stat().st_mtime >= clip.stat().st_mtime:
        return out
    if not build:
        return None
    with PREVIEW_LOCK:
        if out.exists() and out.stat().st_mtime >= clip.stat().st_mtime:
            return out
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        tail = ["-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "24", "-b:v", "0",
                "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
        for hw in (["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], ["-hwaccel", "auto"], []):
            enc = tail if hw else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] + tail[10:]
            r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *hw, "-i", str(clip), *enc],
                               capture_output=True, creationflags=NO_WINDOW | 0x4000)  # BELOW_NORMAL — не мешаем игре
            if r.returncode == 0 and tmp.exists():
                tmp.replace(out)
                break
        else:
            log.error("preview failed: %s", r.stderr.decode("utf-8", "replace")[-300:])
            tmp.unlink(missing_ok=True)
            return None
        # чистим превью удалённых клипов и держим не больше 30 штук
        alive = {f.stem for f in clip.parent.glob("*.mp4")}
        files = sorted(PREVIEW_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        for i, f in enumerate(files):
            if f.stem not in alive or i >= 30:
                f.unlink(missing_ok=True)
        return out


THUMB_DIR = DATA_DIR / "thumbs"


def thumb_for(clip):
    """Картинка-превью клипа (jpg в кэше). Возвращает путь или None."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    t = THUMB_DIR / (clip.stem + ".jpg")
    if not t.exists():
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", "2", "-i", str(clip),
                        "-frames:v", "1", "-vf", "scale=400:-2", "-q:v", "5", str(t)],
                       capture_output=True, creationflags=NO_WINDOW)
    return t if t.exists() else None


def clip_cache_files(stem):
    """Всё, что приложение хранит про клип: превью, H.264-копия, дорожки, метки киллов."""
    files = [THUMB_DIR / f"{stem}.jpg", PREVIEW_DIR / f"{stem}.mp4", DATA_DIR / "meta" / f"{stem}.json"]
    return files + list(TRACK_DIR.glob(f"{stem}__*.m4a"))


def retry(fn, tries=10):
    """Файл может быть секунду занят плеером (медиасервер дочитывает кусок) — пробуем ещё."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.2)


def to_recycle_bin(path):
    """Удаление в корзину Windows (SHFileOperation с FOF_ALLOWUNDO) — случайное удаление можно отменить."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]
    op = SHFILEOPSTRUCTW(wFunc=3, pFrom=str(path) + "\0", fFlags=0x40 | 0x10 | 0x4 | 0x400)  # DELETE, ALLOWUNDO|NOCONFIRM|SILENT|NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"не удалось удалить (код {rc})")


class Api:
    """Методы, которые зовёт страница через window.pywebview.api. Поля приватные — pywebview их не обходит."""

    def __init__(self, app):
        self._app = app

    def init(self):
        a = self._app
        outs, mics = list_devices()
        return {"config": a.cfg, "outs": outs, "mics": mics, "monitors": monitor_list(),
                "output_dir": str(output_dir(a.cfg))}

    def poll(self):
        if not self._app.window_visible():  # окно скрыто — странице хватит редких обновлений
            return {"visible": False, "clips_version": self._app.clips_version}
        a, rec = self._app, self._app.rec
        m = rec.mixer if rec else None
        buffered = int(min(a.cfg["duration_sec"], time.time() - rec.started_at)) if rec and rec.started_at else 0
        return {
            "running": bool(rec and rec.proc and rec.proc.poll() is None),
            "buffered": buffered,
            "duration": a.cfg["duration_sec"],
            "hotkey": a.cfg["hotkey"],
            "hotkey_error": a.hotkey.error if a.hotkey else None,
            "encoder": rec.encoders[rec.mode] if rec else "",
            "sys_level": m.system.level if m and m.system else None,
            "mic_level": m.mic.level if m and m.mic else None,
            "saving": a.saving.locked(),
            "clips_version": a.clips_version,
            "last_error": a.last_error,
            "gsi": a.gsi.status() if a.gsi else None,
            "dc_level": m.discord.level if m and m.discord else None,
            "visible": True,
        }

    def clips(self):
        """Все клипы в папке, новые первыми. Превью — ссылкой, если уже есть; нет — страница попросит thumb()."""
        out = []
        for f in output_dir(self._app.cfg).glob("*.mp4"):
            try:
                st = f.stat()
            except OSError:
                continue
            t = THUMB_DIR / (f.stem + ".jpg")
            out.append({"path": str(f), "name": f.stem, "size": st.st_size, "mtime": st.st_mtime,
                        "auto": clip_meta(f).get("auto"),
                        "time": datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%y %H:%M"),
                        "thumb": self._app.media.url("thumbs", t) if t.exists() else None})
        out.sort(key=lambda c: c["mtime"], reverse=True)
        return out

    def _own(self, path):
        p = Path(path)
        if p.parent != output_dir(self._app.cfg) or p.suffix.lower() != ".mp4" or not p.exists():
            raise ValueError("клип не найден")
        return p

    def thumb(self, path):
        t = thumb_for(self._own(path))
        return self._app.media.url("thumbs", t) if t else None

    def delete_clip(self, path):
        p = self._own(path)
        retry(lambda: to_recycle_bin(p))
        for f in clip_cache_files(p.stem):
            try:
                f.unlink()
            except OSError:
                pass
        log.info("клип удалён в корзину: %s", p.name)
        self._app.clips_version += 1
        return True

    def rename_clip(self, path, new_name):
        import re
        p = self._own(path)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(new_name)).strip().rstrip(". ")
        if not name:
            raise ValueError("пустое имя")
        if name.lower().endswith(".mp4"):
            name = name[:-4]
        dst = p.with_name(name[:150] + ".mp4")
        if dst == p:
            return str(p)
        if dst.exists():
            raise ValueError("клип с таким именем уже есть")
        retry(lambda: p.rename(dst))
        for f in clip_cache_files(p.stem):
            if f.exists():
                try:
                    f.rename(f.with_name(f.name.replace(p.stem, dst.stem, 1)))
                except OSError:
                    pass
        log.info("клип переименован: %s -> %s", p.name, dst.name)
        self._app.clips_version += 1
        return str(dst)

    def set_fullscreen(self, on):
        """Плеер во весь экран: страница развернула элемент — разворачиваем и само окно (WebView2 сам этого не делает)."""
        w = self._app.window
        if bool(getattr(w.native, "is_fullscreen", False)) != bool(on):
            w.toggle_fullscreen()

    def save_clip(self):
        threading.Thread(target=self._app.save, daemon=True).start()
        return True

    def apply(self, cfg):
        new = dict(self._app.cfg)
        new.update(cfg)
        parse_hotkey(new["hotkey"])
        self._app.apply_config(new)
        return True

    def capture_hotkey(self, on):
        self._app.pause_hotkey(on)

    def preview_sound(self, name, volume):
        play_sound(name, volume)

    def pick_sound(self):
        import webview
        r = self._app.window.create_file_dialog(webview.OPEN_DIALOG, file_types=("Звук WAV (*.wav)",))
        return r[0] if r else None

    def check_hotkey(self, combo):
        try:
            parse_hotkey(combo)
            return True
        except ValueError:
            return False

    def open_folder(self):
        os.startfile(output_dir(self._app.cfg))

    def pick_folder(self):
        import webview
        r = self._app.window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(output_dir(self._app.cfg)))
        return r[0] if r else None

    def clip_info(self, path):
        """Данные для встроенного плеера: адрес на локальном медиасервере, fps и длительность."""
        import re
        p = Path(path)
        if p.parent != output_dir(self._app.cfg) or not p.exists():
            return None
        info = clip_codec(p)
        hevc = "Video: hevc" in info
        pv = preview_for(p, build=False) if hevc else None
        fps = re.search(r"(\d+(?:\.\d+)?) fps", info)
        dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
        res = re.search(r", (\d{3,5})x(\d{3,5})", info)
        return {
            "url": self._app.media.url("clips", p),
            "fps": float(fps.group(1)) if fps else 60.0,
            "duration": int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0,
            "res": f"{res.group(1)}×{res.group(2)}" if res else "",
            "codec": "hevc" if hevc else "h264",
            "preview": self._app.media.url("previews", pv) if pv else None,
            "tracks": [{"title": t, "url": self._app.media.url("tracks", f)} for t, f in clip_tracks(p, info)],
            "meta": clip_meta(p),
        }

    def prepare_preview(self, path):
        p = Path(path)
        if p.parent != output_dir(self._app.cfg) or not p.exists():
            return None
        pv = preview_for(p)
        return self._app.media.url("previews", pv) if pv else None

    def log(self, msg):
        """Ошибки страницы (плеер и т.п.) — в общий лог."""
        log.info("page: %s", str(msg)[:500])

    def open_clip(self, path):
        if Path(path).parent == output_dir(self._app.cfg):
            os.startfile(path)

    def reveal_clip(self, path):
        if Path(path).parent == output_dir(self._app.cfg):
            subprocess.Popen(["explorer", "/select,", path])


# ---------------------------------------------------------------- media server

class MediaServer:
    """Локальный http-сервер для встроенного плеера: клипы и превью с поддержкой Range (перемотка).
    Виртуальный хост WebView2 отдаёт видео с ошибкой формата, а bottle внутри pywebview не умеет Range.
    Слушает только 127.0.0.1, а в пути случайный токен — чужие программы адрес не угадают."""

    def __init__(self, roots):
        import secrets
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.roots = roots  # имя → функция, возвращающая папку (папка клипов может смениться в настройках)
        self.token = secrets.token_urlsafe(12)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self._serve(head=True)

            def do_GET(self):
                self._serve(head=False)

            def _serve(self, head):
                from urllib.parse import unquote, urlsplit
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) != 3 or parts[0] != server.token or parts[1] not in server.roots:
                    return self.send_error(404)
                name = unquote(parts[2])
                folder = server.roots[parts[1]]()
                f = folder / name
                if "/" in name or "\\" in name or f.parent != folder or not f.is_file():
                    return self.send_error(404)
                size = f.stat().st_size
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].split(",")[0].partition("-")
                    if a:
                        start, end = int(a), int(b) if b else size - 1
                    elif b:  # bytes=-N — последние N байт
                        start = max(0, size - int(b))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        return self.end_headers()
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                ctype = {".jpg": "image/jpeg", ".m4a": "audio/mp4"}.get(f.suffix.lower(), "video/mp4")
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                # страница живёт на другом порту: без этого fetch() дорожек блокируется (видео-тегу CORS не нужен)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if head:
                    return
                try:
                    with open(f, "rb") as fh:
                        fh.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = fh.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    pass  # плеер перемотал и бросил старый запрос — это нормально

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, root, path):
        from urllib.parse import quote
        return f"http://127.0.0.1:{self.port}/{self.token}/{root}/{quote(path.name)}?{int(path.stat().st_mtime)}"


# ---------------------------------------------------------------- CS2 autoclips (Game State Integration)

GSI_PORT = 3417  # прописан в конфиге CS2, поэтому фиксированный
GSI_CFG_NAME = "gamestate_integration_replaybuffer.cfg"
META_DIR = DATA_DIR / "meta"


def cs2_cfg_dirs():
    """Папки game/csgo/cfg во всех библиотеках Steam."""
    import re
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return []
    libs = {steam}
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            libs.add(Path(m.group(1).replace("\\\\", "\\")))
    out = []
    for lib in libs:
        d = lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg"
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def install_gsi_cfg(token):
    """Кладём конфиг GSI в CS2 — официальный способ Valve получать события игры (VAC его не трогает).
    CS2 читает такие конфиги только при запуске. Возвращает (путь, изменился ли файл)."""
    text = f'''"Replay Buffer"
{{
  "uri"       "http://127.0.0.1:{GSI_PORT}/"
  "timeout"   "1.1"
  "buffer"    "0.0"
  "throttle"  "0.1"
  "heartbeat" "30.0"
  "auth"      {{ "token" "{token}" }}
  "data"
  {{
    "provider"           "1"
    "map"                "1"
    "round"              "1"
    "player_id"          "1"
    "player_state"       "1"
    "player_match_stats" "1"
  }}
}}
'''
    for d in cs2_cfg_dirs():
        f = d / GSI_CFG_NAME
        try:
            if f.exists() and f.read_text(encoding="utf-8") == text:
                return f, False
            f.write_text(text, encoding="utf-8")
            log.info("GSI cfg written: %s", f)
            return f, True
        except OSError as e:
            log.error("GSI cfg: %s", e)
    return None, False


class Gsi:
    """Следит за своими киллами в CS2. Набрал за раунд нужное число — через несколько секунд после
    последнего килла сохраняет клип. Время киллов запоминает, чтобы показать метки на таймлайне клипа."""

    PRE_FIRST, PRE_NEXT, POST = 7.0, 3.0, 5.0  # как на Leetify/scope.gg: до первого килла 7 с, до следующих 3, после — 5
    STREAK_GAP = 15.0  # бой насмерть: пауза дольше — новая серия

    def __init__(self, app):
        self.app = app
        self.kills_total = None
        self.round = None
        self.round_kills = 0
        self.round_list = []   # время каждого нашего килла в текущем раунде
        self.round_clip = None  # автоклип этого раунда: если киллов станет больше, заменим его полным
        self.group = 0          # номер раунда/серии — чтобы заменять клип только своего раунда
        self.phase = None
        self.kill_times = deque(maxlen=200)
        self.timer = None
        self.last_data = 0.0
        self.cfg_path, _ = install_gsi_cfg(app.cfg["gsi_token"])
        self.lock = threading.Lock()
        try:
            self.httpd = self._serve()
        except OSError as e:
            log.error("GSI port %d busy: %s", GSI_PORT, e)
            self.httpd = None

    def _serve(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        gsi = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                except Exception:
                    data = None
                self.send_response(200)
                self.end_headers()
                if data:
                    gsi.update(data)

        httpd = ThreadingHTTPServer(("127.0.0.1", GSI_PORT), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def update(self, d):
        if (d.get("auth") or {}).get("token") != self.app.cfg["gsi_token"]:
            return
        self.last_data = time.time()
        me = (d.get("provider") or {}).get("steamid")
        p = d.get("player") or {}
        mp = d.get("map") or {}
        rnd, mode = mp.get("round"), mp.get("mode")
        phase = (d.get("round") or {}).get("phase")
        with self.lock:
            # Границу раунда ставим по старту следующего (заморозка), а не по смене номера: CS2 увеличивает
            # map.round в момент конца раунда — в том же пакете, что и победный килл, и он уезжал «в новый раунд»
            if phase == "freezetime" and self.phase != "freezetime":
                self._new_group()
                self.round = None
            elif phase == "live":
                if self.round is not None and rnd != self.round:  # заморозку пропустили — номер раунда сменился
                    self._new_group()
                self.round = rnd
            if phase:
                self.phase = phase
            if not me or p.get("steamid") != me:  # умерли и смотрим за другим — его киллы не наши
                return
            kills = (p.get("match_stats") or {}).get("kills")
            if kills is None:
                return
            if self.kills_total is not None and kills > self.kills_total:
                now = time.time()
                # в бою насмерть «раунд» — весь матч: серией считаем киллы, между которыми меньше STREAK_GAP
                if mode == "deathmatch" and self.round_list and now - self.round_list[-1] > self.STREAK_GAP:
                    self._new_group()
                for _ in range(min(kills - self.kills_total, 5)):
                    self.kill_times.append(now)
                    self.round_list.append(now)
                # число киллов считаем сами — ровно те, что попадут в нарезку. Счётчик CS2 round_kills
                # в некоторых режимах не сбрасывается вовремя, и подпись «2k» расходилась с тем, что в клипе
                count = len(self.round_list)
                self.round_kills = count
                log.info("kill: mode=%s phase=%s round=%s cs2_round_kills=%s match_kills=%s -> %d in group",
                         mode, (d.get("round") or {}).get("phase"), rnd,
                         (p.get("state") or {}).get("round_kills"), kills, count)
                cfg = self.app.cfg
                if cfg["autoclip"] and count >= cfg["autoclip_kills"]:
                    if self.timer:
                        self.timer.cancel()
                    # ждём «после килла» + закрытие секундного сегмента; новый килл перезапускает ожидание
                    self.timer = threading.Timer(self.POST + 1.5, self._fire,
                                                 args=(list(self.round_list), count, self.group))
                    self.timer.daemon = True
                    self.timer.start()
            self.kills_total = kills  # смена карты обнуляет счётчик — просто принимаем новое значение

    def _new_group(self):
        """Новый раунд (или новая серия в бою насмерть): киллы и автоклип считаются заново."""
        self.group += 1
        self.round_kills, self.round_list, self.round_clip = 0, [], None

    def windows(self, kills):
        """Окна [начало, конец] вокруг киллов; пересекающиеся сливаются в одно."""
        out = []
        for i, k in enumerate(sorted(kills)):
            ws, we = k - (self.PRE_FIRST if i == 0 else self.PRE_NEXT), k + self.POST
            if out and ws <= out[-1][1]:
                out[-1][1] = max(out[-1][1], we)
            else:
                out.append([ws, we])
        return out

    def _fire(self, kills, count, group):
        with self.lock:
            self.timer = None
        log.info("autoclip: %d kills (group %s)", count, group)
        out = self.app.save_highlight(f"{count}k", self.windows(kills), kills)
        if not out:
            return
        with self.lock:
            old, same_round = self.round_clip, group == self.group
            if same_round:
                self.round_clip = out
        if same_round and old and old != out:  # в раунде добавились киллы — старый короткий клип больше не нужен
            for f in (old, META_DIR / (old.stem + ".json"), DATA_DIR / "thumbs" / (old.stem + ".jpg")):
                try:
                    f.unlink()
                except OSError:
                    pass
            log.info("autoclip replaced: %s -> %s", old.name, out.name)

    def markers(self, end, duration):
        """Секунды от начала клипа, на которых были наши киллы."""
        start = end - duration
        return [round(t - start, 2) for t in self.kill_times if start <= t <= end]

    def status(self):
        alive = time.time() - self.last_data < 45  # heartbeat у CS2 — 30 с
        return {"cfg": str(self.cfg_path) if self.cfg_path else None, "connected": alive,
                "port_ok": self.httpd is not None, "round_kills": self.round_kills if alive else 0}


# ---------------------------------------------------------------- app

def make_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=(28, 28, 32, 255))
    d.ellipse((16 * s, 16 * s, 48 * s, 48 * s), fill=(230, 45, 45, 255))
    return img


def free_port():
    """У pywebview по умолчанию фиксированный порт 42001 — другая программа на pywebview его перехватит."""
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def dark_titlebar(hwnd):
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (новые/старые сборки Win10)
        v = ctypes.c_int(1)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4) == 0:
            break


class App:
    def __init__(self, hidden):
        self.first_run = not CONFIG_PATH.exists()
        self.cfg = load_config()
        if not self.cfg.get("gsi_token"):
            import secrets
            self.cfg["gsi_token"] = secrets.token_hex(12)
            save_config(self.cfg)
        if self.first_run:
            save_config(self.cfg)
        self.hidden = hidden and not self.first_run
        self.rec = None
        self.hotkey = None
        self.window = None
        self.quitting = False
        self.clips_version = 0
        self.last_error = None
        self.saving = threading.Lock()
        self.gsi = None
        self.icon = pystray.Icon("ReplayBuffer", make_icon(), APP_NAME, self._menu())

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self.show(), default=True),
            pystray.MenuItem(lambda _: f"Сохранить клип ({self.cfg['hotkey'].upper()})", lambda: self.save()),
            pystray.MenuItem("Папка с клипами", lambda: os.startfile(output_dir(self.cfg))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Проверить обновления", self.check_updates),
            pystray.MenuItem("Выход", lambda: self.quit()),
        )

    def notify(self, text):
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:
            pass

    def start(self, announce=True):
        BUF_DIR.mkdir(parents=True, exist_ok=True)
        self.rec = Recorder(self.cfg)
        self.rec.start()
        self.hotkey = Hotkey(self.cfg["hotkey"], self.save)
        self.icon.title = f"{APP_NAME} — {self.cfg['hotkey'].upper()} сохраняет {self.cfg['duration_sec']} сек"
        if self.hotkey.error:
            log.error(self.hotkey.error)
            self.notify(f"Ошибка: {self.hotkey.error}. Поменяй хоткей в настройках")
        elif announce and self.hidden:
            self.notify(f"Запись идёт. {self.cfg['hotkey'].upper()} — сохранить последние "
                        f"{self.cfg['duration_sec']} сек")

    def stop(self):
        if self.hotkey:
            self.hotkey.stop()
        if self.rec:
            self.rec.stop()

    def pause_hotkey(self, paused):
        """Пока на странице вводят новое сочетание, глобальный хоткей снят — иначе Windows его перехватит."""
        if paused:
            if self.hotkey:
                self.hotkey.stop()
        elif not self.hotkey or self.hotkey.stopped:
            self.hotkey = Hotkey(self.cfg["hotkey"], self.save)

    def apply_config(self, new):
        try:
            set_autostart(new["autostart"])
        except OSError as e:
            log.error("автозапуск: %s", e)
        save_config(new)
        self.stop()
        self.cfg = new
        self.start(announce=False)

    def save(self, tag=None):
        if not self.saving.acquire(blocking=False):
            return
        try:
            play_sound(self.cfg["save_sound"], self.cfg["sound_volume"])
            end = time.time()
            name = foreground_app_name()
            if tag:  # автоклип: cs2_3k_...
                name = f"cs2_{tag}"
            out = self.rec.save_clip(name)
            span = self.rec.last_span or (end - self.cfg["duration_sec"], end)
            marks = self.gsi.markers(span[1], span[1] - span[0]) if self.gsi else []
            if marks or tag:
                META_DIR.mkdir(parents=True, exist_ok=True)
                (META_DIR / (out.stem + ".json")).write_text(json.dumps({"kills": marks, "auto": tag}), encoding="utf-8")
            size = out.stat().st_size / 2**20
            log.info("клип сохранён: %s (%.1f МБ)", out, size)
            thumb_for(out)
            if self.cfg["codec"] == "hevc":  # превью для встроенного плеера готовим заранее, в фоне
                threading.Thread(target=preview_for, args=(out,), daemon=True).start()
            self.last_error = None
            self.clips_version += 1
            if not self.window_visible():
                self.notify(f"Клип сохранён: {out.name} ({size:.0f} МБ)")
        except Exception as e:
            log.exception("не удалось сохранить клип")
            if self.cfg["save_sound"] != "none":
                play_sound("error", self.cfg["sound_volume"])
            self.last_error = str(e)
            self.notify(f"Не удалось сохранить клип: {e}")
        finally:
            self.saving.release()

    def save_highlight(self, tag, windows, kills):
        """Автоклип CS2: нарезка вокруг киллов раунда. Возвращает путь к клипу или None."""
        with self.saving:  # ручное сохранение в этот момент подождёт — они пишут из одного буфера
            try:
                play_sound(self.cfg["save_sound"], self.cfg["sound_volume"])
                out, marks = self.rec.save_highlight(f"cs2_{tag}", windows, kills)
                META_DIR.mkdir(parents=True, exist_ok=True)
                (META_DIR / (out.stem + ".json")).write_text(json.dumps({"kills": marks, "auto": tag}), encoding="utf-8")
                log.info("автоклип: %s (%.1f МБ, отрезков %d)", out.name, out.stat().st_size / 2**20, len(windows))
                thumb_for(out)
                if self.cfg["codec"] == "hevc":
                    threading.Thread(target=preview_for, args=(out,), daemon=True).start()
                self.clips_version += 1
                return out
            except Exception as e:
                log.exception("автоклип не сохранился")
                self.last_error = str(e)
                return None

    def hwnd(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return 0

    def window_visible(self):
        h = self.hwnd()
        return bool(h and user32.IsWindowVisible(h) and not user32.IsIconic(h))

    def show(self):
        if self.window:
            self.window.show()
            self.window.restore()
            self._webview_memory(low=False)
            h = self.hwnd()
            if h:
                user32.SetForegroundWindow(h)

    def check_updates(self):
        threading.Thread(target=self._check_updates, daemon=True).start()

    def _check_updates(self):
        from urllib.request import urlopen
        import re
        repo = "loading1user/replay-buffer"
        try:
            current = (RES_DIR / "ui" / "VERSION").read_text(encoding="ascii").strip()
            with urlopen(f"https://api.github.com/repos/{repo}/releases/latest", timeout=10) as response:
                release = json.load(response)
            for asset in release["assets"]:
                match = re.fullmatch(r"ReplayBuffer-Setup-(\d+\.\d+\.\d+)-x64\.exe", asset["name"])
                if match:
                    latest = match[1]
                    break
            else:
                raise ValueError("В релизе отсутствует установщик Windows x64")
            if tuple(map(int, latest.split("."))) <= tuple(map(int, current.split("."))):
                user32.MessageBoxW(0, f"Установлена актуальная версия {current}.", APP_NAME, 0x40)
                return
            if user32.MessageBoxW(0, f"Доступна версия {latest}. Скачать установщик?", APP_NAME, 0x24) == 6:
                from urllib.parse import quote
                tag = quote(release["tag_name"], safe="")
                os.startfile(f"https://github.com/{repo}/releases/download/{tag}/{asset['name']}")
        except (OSError, ValueError, KeyError) as e:
            log.warning("Проверка обновлений: %s", e)
            user32.MessageBoxW(0, "Не удалось проверить обновления. Попробуйте позже.", APP_NAME, 0x30)

    def quit(self):
        self.quitting = True
        self.stop()
        self.icon.stop()
        if self.window:
            self.window.destroy()

    def _webview_memory(self, low):
        """Скрытому WebView2 говорим ужаться по памяти."""
        try:
            from System import Action
            from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level
            wv = self.window.native.browser.webview
            level = Level.Low if low else Level.Normal
            wv.Invoke(Action(lambda: setattr(wv.CoreWebView2, "MemoryUsageTargetLevel", level)))
        except Exception as e:
            log.info("webview memory level: %s", e)

    def _on_closing(self):
        if self.quitting:
            return True
        self.window.hide()  # крестик сворачивает в трей, запись продолжается
        self._webview_memory(low=True)
        return False

    def _on_shown(self):
        h = self.hwnd()
        if h:
            dark_titlebar(h)
            for size, kind in ((16, 0), (32, 1)):  # WM_SETICON: ICON_SMALL / ICON_BIG
                ico = user32.LoadImageW(None, str(RES_DIR / "icon.ico"), 1, size, size, 0x10)
                if ico:
                    user32.SendMessageW(h, 0x80, kind, ico)

    def run(self):
        import webview
        self.icon.run_detached()
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        TRACK_DIR.mkdir(parents=True, exist_ok=True)
        self.gsi = Gsi(self)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        self.media = MediaServer({"clips": lambda: output_dir(self.cfg), "previews": lambda: PREVIEW_DIR,
                                  "thumbs": lambda: THUMB_DIR,
                                  "tracks": lambda: TRACK_DIR})
        self.start()
        self.window = webview.create_window(
            APP_NAME, str(RES_DIR / "ui" / "index.html"), js_api=Api(self), width=1080, height=740,
            min_size=(880, 620), background_color="#0d0e12", hidden=self.hidden)
        self.window.events.closing += self._on_closing
        self.window.events.shown += self._on_shown
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(DATA_DIR / "webview"),
                      http_port=free_port())
        self.quitting = True
        self.stop()
        self.icon.stop()


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=DATA_DIR / "replay.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    faulthandler.enable(open(DATA_DIR / "crash.log", "a", encoding="utf-8"))
    kernel32.CreateMutexW(None, False, "ReplayBuffer_single_instance")
    if kernel32.GetLastError() == 183:  # уже запущены — просто показываем окно
        h = user32.FindWindowW(None, APP_NAME)
        if h:
            user32.ShowWindow(h, 9)
            user32.SetForegroundWindow(h)
        return
    try:
        App(hidden="--hidden" in sys.argv).run()
    except Exception:
        log.exception("fatal")
        raise


if __name__ == "__main__":
    main()
