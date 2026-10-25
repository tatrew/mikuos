"""RVC voice conversion worker. Runs under Applio's own Python (its venv has torch + RVC),
never under MikuOS's: started by mikuos.voice.rvc.ApplioConverter, keeps the model loaded.

Protocol: one JSON object per line on stdin -> one JSON line on stdout.
  {"input": "in.wav", "output": "out.npy", "model": "...pth", "index": "...", "pitch": 0, ...}
  -> {"ok": true, "output": "out.npy", "sample_rate": 40000, "seconds": 0.8}
  -> {"ok": false, "error": "..."}
The first line written is {"ready": true} once the engine is imported.
Applio prints progress to stdout, so this script moves that chatter to stderr.
Usage: python applio_worker.py <applio app dir> <applio runtime dir>
"""
import json
import os
import sys
import time
import traceback


def main() -> int:
    app_dir, runtime_dir = (os.path.abspath(p) for p in sys.argv[1:3])
    backend = os.path.join(app_dir, "backend", "applio")
    reply_to = sys.stdout
    sys.stdout = sys.stderr  # engine prints go to the log, not the protocol
    # same environment the Applio CLI (data/runtime/core.py) prepares
    os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
    os.environ.setdefault("APPLIO_ROOT", runtime_dir)
    os.environ.setdefault("APPLIO_CODE_ROOT", runtime_dir)
    os.environ["APPLIO_BACKEND_ROOT"] = backend
    sys.path[:0] = [backend, app_dir]
    os.chdir(runtime_dir)  # relative paths (rvc/models/..., logs/...) resolve like in Applio

    def send(obj):
        reply_to.write(json.dumps(obj, ensure_ascii=False) + "\n")
        reply_to.flush()

    try:
        import numpy as np
        import soundfile as sf
        from backend.python.inference.optimized import install
        install()
        from rvc.infer.infer import VoiceConverter
        converter = VoiceConverter()
    except Exception as e:  # noqa: BLE001 - reported to MikuOS, which falls back to plain Silero
        traceback.print_exc()
        send({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    send({"ready": True})

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            started = time.monotonic()
            wav_out = req["output"] + ".wav"
            converter.convert_audio(
                audio_input_path=req["input"], audio_output_path=wav_out,
                model_path=req["model"], index_path=req.get("index") or "",
                pitch=int(req.get("pitch", 0)), f0_method=req.get("f0_method", "rmvpe"),
                index_rate=float(req.get("index_rate", 0.5)), protect=float(req.get("protect", 0.5)),
                volume_envelope=float(req.get("volume_envelope", 1.0)),
                embedder_model=req.get("embedder_model", "contentvec"), export_format="WAV")
            audio, sr = sf.read(wav_out, dtype="float32", always_2d=True)
            np.save(req["output"], audio.mean(axis=1))
            os.remove(wav_out)
            send({"ok": True, "output": req["output"], "sample_rate": int(sr),
                  "seconds": round(time.monotonic() - started, 3)})
        except Exception as e:  # noqa: BLE001 - one bad request must not kill the worker
            traceback.print_exc()
            send({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
