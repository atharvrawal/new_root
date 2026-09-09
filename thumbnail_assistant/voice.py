"""Local speech-to-text capture: microphone recording + offline
transcription, for the voice-capture feature (Ctrl+Alt+V start/stop).

This module is the voice-capture counterpart to ``capture.py`` (which owns
screenshot capture). Same split of responsibilities: this file only knows
how to record from a microphone, transcribe it, and hand back plain text.
What happens to that text afterwards (framing it and sending it to Gemini
as its own one-shot request) lives in ``ui/app_controller.py``'s
_finish_voice_capture.

Streaming, not record-then-transcribe
--------------------------------------
Earlier version of this module recorded the full clip to a WAV file on
disk, then transcribed it after the second Ctrl+Alt+V press - meaning the
whole speech-to-text latency landed *after* you stopped talking. Instead,
``VoiceRecorder`` now transcribes incrementally off the in-memory audio
buffer while recording is still in progress (see ``_streaming_worker``):
every ``_CHUNK_INTERVAL_S`` seconds it hands whatever's new since the last
pass straight to faster-whisper as a raw ``numpy`` array (no file I/O at
all - faster-whisper's ``transcribe()`` accepts an in-memory array just as
well as a path). By the time you press Ctrl+Alt+V to stop, most of what
you said is usually already transcribed; ``stop()`` only has to flush the
last small tail, not the entire recording.

Trade-off worth knowing: chunking on a fixed interval (rather than on
detected pauses) means a word can occasionally land right on a chunk
boundary and get mis-split. ``vad_filter=True`` (voice-activity filtering,
built into faster-whisper) trims silence within each chunk and reduces how
often this actually bites, but it isn't eliminated. If this shows up in
practice, the fix is lengthening ``_CHUNK_INTERVAL_S`` (fewer boundaries,
more latency) rather than anything structural.

Library choices
----------------
* ``sounddevice`` for microphone I/O: device enumeration (for the
  settings-window picker) plus streaming callback-based capture of
  arbitrary length, which a one-shot ``sounddevice.rec(duration)`` call
  can't do since we don't know the length until the second hotkey press.
* ``faster-whisper`` (CTranslate2-based) for transcription: production-
  quality offline Whisper, meaningfully faster than ``openai-whisper`` at
  the same accuracy, with float16-on-CUDA and an automatic CPU/int8
  fallback if no CUDA runtime is available - see ``_get_model()`` below.
"""
from __future__ import annotations

import logging
import threading
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHANNELS = 1

# "small.en" is the sweet spot for this use case: English-only lecture/
# speech content, needs to run comfortably on a single consumer GPU (a
# 3050-class card) or fall back to CPU without becoming unusably slow.
# Bump to "medium.en" if accuracy matters more than latency for your
# hardware - nothing else here needs to change.
_MODEL_SIZE = "small.en"

# How often the streaming worker transcribes whatever's newly buffered.
# Shorter = lower latency after the final stop, but more chunk-boundary
# word-splitting risk (see module docstring); longer = the reverse.
_CHUNK_INTERVAL_S = 3.0

# Skip a transcription pass if less than this much *new* audio has
# accumulated - avoids spinning the model on a fraction of a second of
# audio every interval tick while you're still mid-sentence.
_MIN_CHUNK_SECONDS = 1.0

_whisper_model = None
_model_lock = threading.Lock()


# -- device enumeration (for the settings window's microphone picker) ------
def list_input_devices() -> List[Tuple[int, str]]:
    """Return ``(device_index, human_readable_label)`` pairs for every
    input-capable audio device currently visible to PortAudio. Never
    raises - returns an empty list on any failure, so the settings window
    can degrade to "use system default" rather than crash.

    The label includes the host API name (e.g. "Windows WASAPI") because
    Windows commonly exposes the same physical microphone multiple times
    under different host APIs (MME, WASAPI, WDM-KS) with the *identical*
    device name - without this, two entries can look indistinguishable in
    the dropdown even though only one of them is the "right" one to use.
    Callers should still key selections by list *position*, not by label
    text, since even the host-API-qualified label isn't guaranteed unique.
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        logger.exception("Could not enumerate audio input devices.")
        return []

    result: List[Tuple[int, str]] = []
    for index, info in enumerate(devices):
        try:
            if info.get("max_input_channels", 0) <= 0:
                continue
            name = info.get("name", f"Device {index}")
            hostapi_index = info.get("hostapi")
            hostapi_name = None
            if hostapi_index is not None and 0 <= hostapi_index < len(hostapis):
                hostapi_name = hostapis[hostapi_index].get("name")
            label = f"{name} ({hostapi_name})" if hostapi_name else name
            result.append((index, label))
        except Exception:
            continue
    return result


# -- desktop-audio (WASAPI loopback via PyAudioWPatch, always-on) -----------
def get_stereo_mix_input_device() -> Optional[int]:
    """Return the device index to use for desktop-audio loopback capture.

    Name kept as-is (despite no longer being Stereo-Mix-specific, and now
    despite not even being a sounddevice device index - see below) so its
    caller (AppController.toggle_voice_capture) doesn't need to change.

    Resolved via PyAudioWPatch - a WASAPI-loopback-patched PyAudio build -
    rather than sounddevice/PortAudio, which has no WASAPI loopback support
    at all (a real, still-open upstream limitation:
    https://github.com/spatialaudio/python-sounddevice/issues/281 - an
    earlier version of this function incorrectly assumed
    sd.WasapiSettings(loopback=True) existed; it does not, on any
    sounddevice version).

    The returned index is a PyAudioWPatch device index, not a sounddevice
    one - the two libraries number devices independently.
    _start_desktop_capture() below always opens it via PyAudioWPatch;
    sounddevice is never used for the desktop-audio side.

    Returns ``None`` if PyAudioWPatch isn't installed, or no default
    WASAPI output/loopback device can be resolved - desktop-audio capture
    then silently stays off for this session (mic-only), same tolerant
    behavior as before.
    """
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        logger.info(
            "PyAudioWPatch is not installed; desktop-audio loopback capture "
            "unavailable this session, continuing mic-only. Install it with "
            "'pip install PyAudioWPatch'."
        )
        return None

    p = pyaudio.PyAudio()
    try:
        wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

        if not default_speakers.get("isLoopbackDevice"):
            for loopback in p.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    default_speakers = loopback
                    break
            else:
                logger.info(
                    "No matching WASAPI loopback device found for default output "
                    "device %r; desktop-audio capture unavailable this session, "
                    "continuing mic-only.",
                    default_speakers.get("name"),
                )
                return None

        logger.info(
            "Using WASAPI loopback device for desktop-audio capture: "
            "index=%s, name=%r",
            default_speakers["index"], default_speakers.get("name"),
        )
        return default_speakers["index"]
    except Exception:
        logger.exception(
            "Could not resolve a WASAPI loopback device for desktop-audio capture; "
            "continuing mic-only."
        )
        return None
    finally:
        p.terminate()


def _resample_linear(audio, orig_sr: int, target_sr: int):
    """Cheap linear-interpolation resampler - no extra dependency (scipy)
    beyond numpy, which this module already requires. Whisper's own
    accuracy tolerance easily absorbs the mild quality loss vs. a proper
    windowed resampler; this only needs to get desktop audio (captured at
    the device's native rate, commonly 44.1/48kHz) down to the 16kHz mono
    everything else in this module assumes."""
    import numpy as np

    if orig_sr == target_sr or audio.size == 0:
        return audio
    duration = audio.shape[0] / orig_sr
    target_len = max(1, int(round(duration * target_sr)))
    orig_positions = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    target_positions = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(target_positions, orig_positions, audio).astype(np.float32)


# -- transcription ---------------------------------------------------------
_cuda_dll_dirs_registered = False


def _register_nvidia_dll_dirs() -> None:
    """Make CUDA/cuBLAS/cuDNN DLLs discoverable on Windows, regardless of
    which of the several common ways they ended up installed.

    Root cause of the "Library cublas64_12.dll is not found" error: these
    DLLs are resolved lazily by ctranslate2, at the moment of the first
    actual CUDA computation - not when the model object is constructed.
    Windows' default DLL search path has no reason to include wherever
    cuBLAS happens to live, so that first lookup fails.

    Checks the common install locations rather than assuming one:
      1. pip nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels
      2. torch's bundled copies, if torch is installed
      3. A system CUDA Toolkit install (CUDA_PATH env vars, or the default
         Program Files location)

    Registers every directory found via os.add_dll_directory. Safe no-op
    on non-Windows platforms.
    """
    global _cuda_dll_dirs_registered
    if _cuda_dll_dirs_registered:
        return
    _cuda_dll_dirs_registered = True

    import glob
    import importlib.util
    import os
    import pathlib
    import sys

    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return

    candidate_dirs: list = []

    # PyInstaller-frozen case: nvidia-cublas-cu12/nvidia-cudnn-cu12 are never
    # actually `import`ed anywhere in this codebase (only their DLLs are
    # loaded, via ctypes, at the point ctranslate2 needs them) - so
    # PyInstaller has no reason to preserve them as a real importable
    # package inside the bundle, even if the .spec file's
    # collect_dynamic_libs() call bundles the DLL files themselves.
    # importlib.util.find_spec("nvidia") below therefore can't be relied on
    # once frozen. collect_dynamic_libs() places bundled binaries at the
    # bundle root (sys._MEIPASS in --onefile mode, or the executable's own
    # directory in --onedir mode) - check both directly instead.
    if getattr(sys, "frozen", False):
        for frozen_root in {getattr(sys, "_MEIPASS", None), str(pathlib.Path(sys.executable).parent)}:
            if frozen_root and pathlib.Path(frozen_root).is_dir():
                candidate_dirs.append(pathlib.Path(frozen_root))

    try:
        spec = importlib.util.find_spec("nvidia")
        if spec is not None and spec.submodule_search_locations:
            for nvidia_root in spec.submodule_search_locations:
                nvidia_root = pathlib.Path(nvidia_root)
                if not nvidia_root.is_dir():
                    continue
                for component_dir in nvidia_root.iterdir():
                    bin_dir = component_dir / "bin"
                    if bin_dir.is_dir():
                        candidate_dirs.append(bin_dir)
    except Exception:
        logger.exception("Error while scanning for pip-installed NVIDIA DLL directories.")

    try:
        spec = importlib.util.find_spec("torch")
        if spec is not None and spec.submodule_search_locations:
            for torch_root in spec.submodule_search_locations:
                lib_dir = pathlib.Path(torch_root) / "lib"
                if lib_dir.is_dir():
                    candidate_dirs.append(lib_dir)
    except Exception:
        logger.exception("Error while scanning for torch's bundled CUDA DLL directory.")

    try:
        env_bins = []
        for env_var, value in os.environ.items():
            if env_var.upper().startswith("CUDA_PATH") and value:
                env_bins.append(pathlib.Path(value) / "bin")
        for bin_dir in env_bins:
            if bin_dir.is_dir():
                candidate_dirs.append(bin_dir)

        if not env_bins:
            for bin_dir in glob.glob(
                r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin"
            ):
                candidate_dirs.append(pathlib.Path(bin_dir))
    except Exception:
        logger.exception("Error while scanning for a system CUDA Toolkit install.")

    if not candidate_dirs:
        logger.debug(
            "No candidate CUDA/cuBLAS DLL directories found (checked pip nvidia-*-cu12 "
            "wheels, torch's bundled libs, and CUDA_PATH/Program Files)."
        )
        return

    found_cublas = False
    registered = []
    for bin_dir in candidate_dirs:
        try:
            os.add_dll_directory(str(bin_dir))
            registered.append(str(bin_dir))
            if any((bin_dir / name).is_file() for name in ("cublas64_12.dll", "cublas64_11.dll")):
                found_cublas = True
        except (OSError, FileNotFoundError):
            continue

    logger.debug(
        "Registered %d CUDA DLL director%s for the process%s: %s",
        len(registered),
        "y" if len(registered) == 1 else "ies",
        " (cublas64_12.dll located in one of them)" if found_cublas
        else " (cublas64_12.dll NOT found in any of them)",
        registered,
    )


def _cuda_runtime_usable() -> bool:
    """Return whether CUDA inference can actually run on this machine -
    not just whether a CUDA-capable GPU exists, but whether the cuBLAS
    runtime library ctranslate2 needs at inference time is actually
    loadable right now.

    A direct test (ctypes attempts to load cublas64_12.dll), not a guess
    based on installed packages. WhisperModel(..., device="cuda") itself
    succeeds even when cuBLAS is missing - only the first actual
    computation touches cuBLAS and fails - so probing at construction time
    doesn't catch this.
    """
    import sys

    if sys.platform != "win32":
        return True  # this failure mode is Windows-DLL-search-path specific

    _register_nvidia_dll_dirs()

    import ctypes

    for dll_name in ("cublas64_12.dll", "cublas64_11.dll"):
        try:
            ctypes.WinDLL(dll_name)
            return True
        except OSError:
            continue
    return False


def _load_whisper_model(WhisperModel, device: str, compute_type: str):
    """Construct a WhisperModel, preferring a fully offline load.

    faster_whisper/huggingface_hub otherwise contacts the Hub on every
    load - even when the model is already cached locally. Try
    local_files_only=True first, only falling back to allowing a download
    if that fails (i.e. genuinely the first run)."""
    try:
        return WhisperModel(_MODEL_SIZE, device=device, compute_type=compute_type, local_files_only=True)
    except Exception:
        logger.info(
            "Model '%s' not yet cached locally; downloading (one-time - later "
            "runs will load fully offline).",
            _MODEL_SIZE,
        )
        return WhisperModel(_MODEL_SIZE, device=device, compute_type=compute_type)


def _get_model():
    """Lazily load and cache the faster-whisper model for the lifetime of
    the process - loading it is the expensive part; every transcription
    call (chunked or final) reuses the same in-memory model.

    Picks CUDA vs. CPU once, up front, based on whether cuBLAS is actually
    loadable (see _cuda_runtime_usable()) - rather than trying CUDA,
    letting the first real transcription call fail, and reloading on CPU
    after the fact."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error(
            "faster-whisper is not installed; cannot transcribe. "
            "Install it with 'pip install faster-whisper'."
        )
        return None

    with _model_lock:
        if _whisper_model is not None:
            return _whisper_model

        if _cuda_runtime_usable():
            try:
                _whisper_model = _load_whisper_model(WhisperModel, "cuda", "float16")
                logger.info("Loaded faster-whisper model '%s' on CUDA.", _MODEL_SIZE)
                return _whisper_model
            except Exception:
                logger.warning(
                    "cuBLAS is loadable but CUDA model construction still failed "
                    "(unusual - possibly a GPU/driver issue rather than a missing "
                    "library); falling back to CPU.", exc_info=True
                )
        else:
            logger.info(
                "cuBLAS runtime (cublas64_12.dll/cublas64_11.dll) is not loadable on "
                "this machine - using CPU for speech-to-text. Install the CUDA "
                "Toolkit, or 'pip install nvidia-cublas-cu12 nvidia-cudnn-cu12', to "
                "enable GPU transcription."
            )

        try:
            _whisper_model = _load_whisper_model(WhisperModel, "cpu", "int8")
            logger.info("Loaded faster-whisper model '%s' on CPU.", _MODEL_SIZE)
        except Exception:
            logger.exception("Failed to load faster-whisper model on CPU.")
            return None
    return _whisper_model


def _transcribe_array(audio, sample_rate: int = _SAMPLE_RATE) -> Optional[str]:
    """Transcribe an in-memory float32 mono numpy array directly - no file
    I/O. Returns the text, or ``None`` on failure/empty result. Never
    raises."""
    model = _get_model()
    if model is None:
        return None
    try:
        segments, _info = model.transcribe(audio, language="en", vad_filter=True)
        text = "".join(segment.text for segment in segments).strip()
        return text or None
    except Exception:
        logger.exception("Transcription failed for an audio chunk.")
        return None


# -- recording + streaming transcription ------------------------------------
class VoiceRecorder:
    """Owns at most one in-progress recording at a time, and transcribes
    it incrementally *while recording is still happening* rather than
    only after it stops - see module docstring.

    Driven entirely by :meth:`start`/:meth:`stop` - there is no fixed
    duration, matching the press-to-start / press-again-to-stop workflow.
    Concurrent start/stop from multiple threads is guarded by
    ``_stop_lock`` here and by ``_voice_lock`` in
    ``AppController.toggle_voice_capture()``, its only caller.
    """

    def __init__(self) -> None:
        self._stream = None
        self._buffer_lock = threading.Lock()
        self._chunks: List = []          # raw float32 arrays from the callback
        self._processed_samples = 0      # how much of the concatenated buffer we've already transcribed
        self._transcript_parts: List[str] = []
        self._recording = False
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()
        # Guards the check-and-clear in stop() below. BrowserManager runs
        # each stop() call on its own freshly-spawned thread (so the
        # hotkey listener never blocks), so if the hotkey fires twice in
        # quick succession (key-repeat, double-fire), two threads can
        # reach stop() concurrently. Without this, both could pass the
        # "is_recording" check before either clears it, and then race on
        # the same self._stream (one sets it to None while the other is
        # still calling .close() on it).
        self._stop_lock = threading.Lock()

        # Desktop-audio capture (Stereo Mix or equivalent, muxed with the mic) -
        # entirely separate stream/buffer/lock from the mic's, since it
        # runs at a different, device-native sample rate and channel
        # count and is resampled down to match the mic before mixing (see
        # _transcribe_pending). None of this activates unless a
        # desktop_device_index is actually passed to start().
        self._desktop_stream = None
        self._desktop_pyaudio = None  # PyAudioWPatch PyAudio() instance backing _desktop_stream
        self._desktop_buffer_lock = threading.Lock()
        self._desktop_chunks: List = []
        self._desktop_processed_samples = 0
        self._desktop_samplerate: Optional[int] = None
        self._desktop_channels: int = 1

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, device_index: Optional[int] = None, desktop_device_index: Optional[int] = None) -> bool:
        """Begin recording from ``device_index`` (or the system default
        input device if ``None``). If ``desktop_device_index`` is given,
        also captures that Stereo Mix (or equivalent) device's audio and
        mixes it with the mic in every transcribed chunk - see
        :meth:`_transcribe_pending`. Leaving it ``None`` is mic-only.
        Returns ``True`` on success; a desktop-capture failure is logged
        and simply leaves you with mic-only for this session rather than
        failing the whole recording."""
        if self._recording:
            logger.warning("VoiceRecorder.start() called while already recording; ignoring.")
            return False

        try:
            import sounddevice as sd
        except ImportError:
            logger.error(
                "sounddevice is not installed; cannot record audio. "
                "Install it with 'pip install sounddevice'."
            )
            return False

        self._chunks = []
        self._processed_samples = 0
        self._transcript_parts = []
        self._desktop_chunks = []
        self._desktop_processed_samples = 0
        self._stop_worker.clear()

        def _callback(indata, frames, time_info, status):
            if status:
                logger.debug("Audio input status: %s", status)
            with self._buffer_lock:
                self._chunks.append(indata.copy())

        try:
            self._stream = sd.InputStream(
                samplerate=_SAMPLE_RATE,
                channels=_CHANNELS,
                dtype="float32",
                device=device_index,
                callback=_callback,
            )
            self._stream.start()
        except Exception:
            logger.exception(
                "Failed to start audio input stream (device_index=%s).", device_index
            )
            self._stream = None
            return False

        # Log exactly which physical device PortAudio actually opened -
        # `device_index=None` above just means "we asked for the default";
        # this confirms what that resolved to, independent of anything
        # settings/config believes was selected.
        try:
            resolved_index = self._stream.device
            info = sd.query_devices(resolved_index)
            logger.info(
                "Audio input stream opened: requested device_index=%s, "
                "resolved device_index=%s, name=%r, default_samplerate=%s, "
                "max_input_channels=%s",
                device_index,
                resolved_index,
                info.get("name"),
                info.get("default_samplerate"),
                info.get("max_input_channels"),
            )
        except Exception:
            logger.exception("Could not query resolved input device info.")

        if desktop_device_index is not None:
            self._start_desktop_capture(sd, desktop_device_index)

        self._recording = True
        self._worker_thread = threading.Thread(
            target=self._streaming_worker, name="VoiceStreamingSTTThread", daemon=True
        )
        self._worker_thread.start()
        logger.info("Voice recording started (device_index=%s).", device_index)
        return True

    def _start_desktop_capture(self, sd, desktop_device_index: int) -> None:
        """Open the desktop-audio (WASAPI loopback) stream via
        PyAudioWPatch. Failure here is intentionally non-fatal to the
        overall recording - see :meth:`start`.

        Uses PyAudioWPatch rather than sounddevice for this stream
        specifically, since sounddevice/PortAudio has no WASAPI loopback
        support at all (see get_stereo_mix_input_device()'s docstring).
        The ``sd`` parameter is accepted for call-site symmetry with the
        mic stream (see :meth:`start`) but is unused here.
        """
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            logger.error(
                "PyAudioWPatch is not installed; cannot start desktop-audio capture."
            )
            self._desktop_stream = None
            return

        import numpy as np

        try:
            self._desktop_pyaudio = pyaudio.PyAudio()
            device_info = self._desktop_pyaudio.get_device_info_by_index(desktop_device_index)
            samplerate = int(device_info.get("defaultSampleRate") or _SAMPLE_RATE)
            channels = max(1, min(int(device_info.get("maxInputChannels") or 2), 2))

            def _desktop_callback(in_data, frame_count, time_info, status):
                try:
                    samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                    samples = samples.reshape(-1, channels) if channels > 1 else samples.reshape(-1, 1)
                    with self._desktop_buffer_lock:
                        self._desktop_chunks.append(samples)
                except Exception:
                    logger.exception("Error in desktop-audio (WASAPI loopback) callback.")
                return (None, pyaudio.paContinue)

            self._desktop_stream = self._desktop_pyaudio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=samplerate,
                input=True,
                input_device_index=desktop_device_index,
                stream_callback=_desktop_callback,
            )
            self._desktop_samplerate = samplerate
            self._desktop_channels = channels
            logger.info(
                "Desktop-audio (WASAPI loopback via PyAudioWPatch) stream opened: "
                "device_index=%s, name=%r, samplerate=%d, channels=%d",
                desktop_device_index,
                device_info.get("name"),
                samplerate,
                channels,
            )
        except Exception:
            logger.exception(
                "Failed to start desktop-audio (WASAPI loopback) stream "
                "(device_index=%s); continuing with mic-only capture for this session.",
                desktop_device_index,
            )
            self._desktop_stream = None
            self._desktop_samplerate = None
            if self._desktop_pyaudio is not None:
                try:
                    self._desktop_pyaudio.terminate()
                except Exception:
                    logger.exception("Error terminating desktop-audio PyAudio instance after failed start.")
                self._desktop_pyaudio = None

    def stop(self) -> Optional[str]:
        """Stop recording, flush any not-yet-transcribed tail audio, and
        return the full transcript assembled so far - or ``None`` if
        nothing was captured/transcribed. Because most of the recording
        was already transcribed incrementally while you were still
        talking, this only has to process the last ``_CHUNK_INTERVAL_S``
        seconds or less, not the whole clip."""
        with self._stop_lock:
            if not self._recording or self._stream is None:
                logger.warning("VoiceRecorder.stop() called while not recording; ignoring.")
                return None
            stream = self._stream
            self._stream = None
            self._recording = False

        self._stop_worker.set()
        try:
            stream.stop()
            stream.close()
        except Exception:
            logger.exception("Error stopping audio input stream.")

        if self._desktop_stream is not None:
            try:
                # PyAudio streams (not sounddevice) use stop_stream()/close(),
                # not stop()/close() - different library, different API.
                self._desktop_stream.stop_stream()
                self._desktop_stream.close()
            except Exception:
                logger.exception("Error stopping desktop-audio stream.")
            finally:
                self._desktop_stream = None

            if self._desktop_pyaudio is not None:
                try:
                    self._desktop_pyaudio.terminate()
                except Exception:
                    logger.exception("Error terminating desktop-audio PyAudio instance.")
                finally:
                    self._desktop_pyaudio = None

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=_CHUNK_INTERVAL_S + 5.0)
            self._worker_thread = None

        # Final flush: transcribe whatever's left that the periodic worker
        # hadn't gotten to yet (at most one interval's worth of audio).
        self._transcribe_pending(final=True)

        transcript = " ".join(part.strip() for part in self._transcript_parts if part.strip()).strip()
        self._chunks = []
        self._transcript_parts = []

        if not transcript:
            logger.warning("Voice recording stopped with no transcribable audio captured.")
            return None

        logger.info("Voice recording stopped; transcript assembled from streaming chunks.")
        return transcript

    # -- internals -----------------------------------------------------
    def _streaming_worker(self) -> None:
        """Runs for the lifetime of the recording, transcribing newly
        buffered audio every _CHUNK_INTERVAL_S seconds so the bulk of the
        speech is already turned into text before stop() is ever called."""
        while not self._stop_worker.wait(_CHUNK_INTERVAL_S):
            self._transcribe_pending(final=False)

    def _get_new_desktop_audio(self, np, target_len: int):
        """Return up to ``target_len`` samples of newly buffered desktop
        audio, downmixed to mono and resampled to ``_SAMPLE_RATE`` to
        match the mic buffer it's about to be mixed with. Returns
        ``None`` if no desktop stream is active or nothing new has
        arrived yet."""
        if self._desktop_stream is None and not self._desktop_chunks:
            return None

        with self._desktop_buffer_lock:
            if not self._desktop_chunks:
                return None
            raw = np.concatenate(self._desktop_chunks, axis=0)
            new_raw = raw[self._desktop_processed_samples:]
            self._desktop_processed_samples = raw.shape[0]

        if new_raw.shape[0] == 0:
            return None

        mono = new_raw.mean(axis=1) if new_raw.ndim == 2 and new_raw.shape[1] > 1 else new_raw.reshape(-1)
        resampled = _resample_linear(mono, self._desktop_samplerate or _SAMPLE_RATE, _SAMPLE_RATE)

        # The mic and desktop streams are independent hardware clocks, so
        # their sample counts for "the same wall-clock interval" won't
        # match exactly even after resampling - trim/pad to the mic
        # chunk's length so the two can be summed sample-for-sample.
        if resampled.shape[0] > target_len:
            return resampled[:target_len]
        if resampled.shape[0] < target_len:
            return np.pad(resampled, (0, target_len - resampled.shape[0]))
        return resampled

    def _transcribe_pending(self, final: bool) -> None:
        try:
            import numpy as np
        except ImportError:
            logger.error("numpy is not installed; cannot process recorded audio.")
            return

        with self._buffer_lock:
            if not self._chunks:
                return
            audio = np.concatenate(self._chunks, axis=0).reshape(-1)
            new_audio = audio[self._processed_samples:]
            if not final and len(new_audio) < int(_MIN_CHUNK_SECONDS * _SAMPLE_RATE):
                # Not enough new audio yet to bother with a model call -
                # leave it for the next tick (or the final flush).
                return
            self._processed_samples = len(audio)

        if len(new_audio) == 0:
            return

        desktop_audio = self._get_new_desktop_audio(np, new_audio.shape[0])
        if desktop_audio is not None:
            # Simple additive mix, then clip to the valid float32 audio
            # range - both sources are typically not loud simultaneously
            # (you talking vs. whatever's playing), so plain summation
            # sounds fine in practice; clipping only bites in the rare
            # case both peak at once, which is a minor, acceptable
            # trade-off against the complexity of dynamic gain balancing.
            mixed_audio = np.clip(new_audio + desktop_audio, -1.0, 1.0).astype(np.float32)
        else:
            mixed_audio = new_audio

        text = _transcribe_array(mixed_audio.copy())
        if text:
            self._transcript_parts.append(text)


# -- message formatting ------------------------------------------------------
DEFAULT_STT_TEMPLATE = (
    "Below is speech-to-text content.\n\n"
    "The transcription was generated locally and may contain small "
    "recognition errors. Please infer the intended meaning where "
    "appropriate.\n\n"
    "{transcript}\n"
)


def build_stt_message(transcript: str, template: Optional[str] = None) -> str:
    """Wrap a raw transcript in STT framing text before sending it to
    Gemini. ``template`` is an optional
    user-customized template (set via the settings window) containing a
    literal ``{transcript}`` placeholder; falls back to
    ``DEFAULT_STT_TEMPLATE`` if not given. If a custom template is missing
    the placeholder, the transcript is appended after it rather than
    silently dropped."""
    template = template or DEFAULT_STT_TEMPLATE
    if "{transcript}" in template:
        return template.format(transcript=transcript)
    return f"{template}\n\n{transcript}\n"
