"""Regression coverage for MP3s FFprobe mistakenly identifies as video."""

from contextlib import contextmanager
import json

import pytest

from app.subprocess_capture import BoundedProcessResult, ProcessCancelled
import app.audiobook_verification as audio
import app.verifier as verifier


def _response(payload, *, code=0):
    return BoundedProcessResult(code, json.dumps(payload).encode("utf-8"), b"")


VIDEO = {
    "format": {"format_name": "vvc", "size": "4820495"},
    "streams": [{"index": 0, "codec_name": "vvc", "codec_type": "video"}],
}
MP3 = {
    "format": {"format_name": "mp3", "duration": "256.757551"},
    "streams": [{
        "index": 0, "codec_name": "mp3", "codec_type": "audio",
        "sample_rate": "44100", "channels": 2,
    }],
}


def _id3_mp3(tmp_path, *, name="chapter.mp3"):
    path = tmp_path / name
    path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"x" * 5000)
    return path


def test_id3_mp3_video_misidentification_recovers_after_full_decode(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    commands = []

    def run(cmd, **kwargs):
        commands.append(list(cmd))
        assert kwargs["cancel_check"] is None
        assert kwargs["timeout"] > 0
        assert kwargs["stderr_limit"] > 0
        if cmd[0] == "ffmpeg":
            assert "-xerror" in cmd
            assert cmd[-2:] == ["null", "-"]
            return BoundedProcessResult(0, b"", b"")
        return _response(MP3 if "-f" in cmd else VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", run)

    probe = audio.probe_audio_file(str(path))
    assert probe["audio_stream_count"] == 1
    assert probe["codec"] == "mp3"
    assert probe["format_name"] == "mp3"
    assert probe["duration_seconds"] == pytest.approx(256.757551)
    assert probe["mp3_fallback_checked"] is True
    assert probe["mp3_fallback_verified"] is True
    assert len(commands) == 3
    assert commands[0][0] == "ffprobe" and "-f" not in commands[0]
    assert commands[1][0] == "ffprobe" and commands[1][commands[1].index("-f")+1] == "mp3"
    assert commands[2][0] == "ffmpeg"


def test_recovery_fails_closed_when_full_decode_fails(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    commands = []

    def run(cmd, **kwargs):
        commands.append(list(cmd))
        if cmd[0] == "ffmpeg":
            return BoundedProcessResult(1, b"", b"invalid MP3 frame")
        return _response(MP3 if "-f" in cmd else VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", run)
    probe = audio.probe_audio_file(str(path))
    assert probe["audio_stream_count"] == 0
    assert probe["format_name"] == "vvc"
    assert probe["mp3_fallback_checked"] is True
    assert probe["mp3_fallback_verified"] is False
    assert len(commands) == 3


def test_recovery_rejects_wrong_codec_even_with_mp3_demuxer(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd[0])
        if "-f" in cmd:
            return _response(VIDEO)
        return _response(VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", run)
    probe = audio.probe_audio_file(str(path))
    assert probe["audio_stream_count"] == 0
    assert probe["mp3_fallback_checked"] is True
    assert commands == ["ffprobe", "ffprobe"]


def test_non_id3_or_non_mp3_never_forces_mp3(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(audio, "run_bounded_process", lambda cmd, **kw: (
        commands.append(cmd) or _response(VIDEO)
    ))
    for name, header in (("chapter.mp3", b"not-ID3"), ("chapter.m4b", b"ID3")):
        path = tmp_path / name
        path.write_bytes(header + b"x" * 5000)
        probe = audio.probe_audio_file(str(path))
        assert probe["audio_stream_count"] == 0
        assert probe["mp3_fallback_checked"] is False
    assert len(commands) == 2


def test_legacy_zero_audio_cache_rechecked_once_and_replaced(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    old = {"path": str(path), "format_name": "vvc", "audio_stream_count": 0}
    saved = []

    def cached(*args):
        return saved[-1] if saved else dict(old)

    monkeypatch.setattr(audio, "cached_probe", cached)
    monkeypatch.setattr(audio, "save_probe", lambda *args: saved.append(dict(args[-1])) or True)
    monkeypatch.setattr(audio, "run_bounded_process", lambda cmd, **kw: (
        BoundedProcessResult(0, b"", b"") if cmd[0] == "ffmpeg"
        else _response(MP3 if "-f" in cmd else VIDEO)
    ))

    first, hit = audio.cached_or_probe_audio_file(str(path))
    assert hit is False
    assert first["audio_stream_count"] == 1
    assert len(saved) == 1
    second, hit = audio.cached_or_probe_audio_file(str(path))
    assert hit is True
    assert second["audio_stream_count"] == 1
    assert len(saved) == 1


def test_failed_legacy_mp3_cache_rechecked_only_once(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    old = {"path": str(path), "format_name": "vvc", "audio_stream_count": 0}
    saved = []
    commands = []
    monkeypatch.setattr(audio, "cached_probe", lambda *args: saved[-1] if saved else dict(old))
    monkeypatch.setattr(audio, "save_probe", lambda *args: saved.append(dict(args[-1])) or True)

    def run(cmd, **kw):
        commands.append(cmd[0])
        if cmd[0] == "ffmpeg":
            return BoundedProcessResult(1, b"", b"invalid")
        return _response(MP3 if "-f" in cmd else VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", run)
    assert audio.cached_or_probe_audio_file(str(path))[1] is False
    assert len(commands) == 3
    assert audio.cached_or_probe_audio_file(str(path))[1] is True
    assert len(commands) == 3


def test_recovery_propagates_cancel(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    def run(cmd, **kw):
        if "-f" in cmd:
            raise ProcessCancelled("cancel")
        return _response(VIDEO)
    monkeypatch.setattr(audio, "run_bounded_process", run)
    with pytest.raises(audio.AudiobookVerificationCancelled):
        audio.probe_audio_file(str(path), cancel_check=lambda: False)


def test_old_no_audio_proof_reopened_once_but_listing_keeps_evidence(monkeypatch):
    current = {
        "id": 1, "source": "audiobook-technical", "verdict": "UNSAFE_FILE",
        "evidence_json": json.dumps({
            "technical": {"reasons": ["chapter.mp3: container has no audio stream."]}
        }),
    }
    class Conn:
        def execute(self, *args):
            return self
        def fetchone(self):
            return dict(current)

    @contextmanager
    def fake_conn():
        yield Conn()

    monkeypatch.setattr(verifier, "init_verification_db", lambda: None)
    monkeypatch.setattr(verifier, "local_conn", fake_conn)
    monkeypatch.setattr(verifier, "_verification_signature", lambda *args: "sig")

    result = {"format": "audiobook"}
    assert verifier._verification_for_fingerprint(result, "path", "fingerprint") is None
    assert verifier._verification_for_fingerprint(
        result, "path", "fingerprint", allow_transient_retry=False
    ) is not None
    current["evidence_json"] = json.dumps({
        "audiobookProbeRecoveryVersion": 1,
        "technical": {"reasons": ["chapter.mp3: container has no audio stream."]},
    })
    assert verifier._verification_for_fingerprint(result, "path", "fingerprint") is not None


def test_ordinary_audiobook_proof_not_invalidated(monkeypatch):
    row = {
        "id": 2, "source": "audiobook-evidence", "verdict": "VERIFIED_CORRECT",
        "evidence_json": json.dumps({"technical": {"reasons": ["ok"]}}),
    }
    class Conn:
        def execute(self, *args):
            return self
        def fetchone(self):
            return row

    @contextmanager
    def fake_conn():
        yield Conn()

    monkeypatch.setattr(verifier, "init_verification_db", lambda: None)
    monkeypatch.setattr(verifier, "local_conn", fake_conn)
    monkeypatch.setattr(verifier, "_verification_signature", lambda *args: "sig")
    assert verifier._verification_for_fingerprint(
        {"format": "audiobook"}, "path", "fingerprint"
    ) is not None

def test_fallback_timeout_is_retryable_and_does_not_replace_cached_probe(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    original_cache = {"path": str(path), "format_name": "vvc", "audio_stream_count": 0}
    saved = []
    commands = []
    fail_once = [True]

    monkeypatch.setattr(audio, "cached_probe", lambda *args: saved[-1] if saved else dict(original_cache))
    monkeypatch.setattr(audio, "save_probe", lambda *args: saved.append(dict(args[-1])) or True)

    def runner(cmd, **kwargs):
        commands.append(list(cmd))
        if cmd[0] == "ffmpeg":
            if fail_once[0]:
                fail_once[0] = False
                raise __import__("subprocess").TimeoutExpired(cmd, 120)
            return BoundedProcessResult(0, b"", b"")
        return _response(MP3 if "-f" in cmd else VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", runner)
    failed, hit = audio.cached_or_probe_audio_file(str(path))
    assert hit is False
    assert failed["transient_probe_failure"] is True
    assert "TimeoutExpired" in failed["probe_error"]
    assert saved == []
    assert audio.summarize_audiobook_verification([failed])["verdict"] == "INCONCLUSIVE"
    assert audio.summarize_audiobook_verification([failed])["reason_code"] == "AUDIO_PROBE_TRANSIENT_FAILURE"

    good, hit = audio.cached_or_probe_audio_file(str(path))
    assert hit is False
    assert good["audio_stream_count"] == 1
    assert good["mp3_fallback_verified"] is True
    assert len(saved) == 1
    assert audio.cached_or_probe_audio_file(str(path))[1] is True


def test_ffmpeg_tool_outage_does_not_cache_or_prove_unsafe_file(tmp_path, monkeypatch):
    path = _id3_mp3(tmp_path)
    saved = []
    monkeypatch.setattr(audio, "cached_probe", lambda *args: None)
    monkeypatch.setattr(audio, "save_probe", lambda *args: saved.append(args) or True)

    def runner(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            raise FileNotFoundError("ffmpeg temporarily unavailable")
        return _response(MP3 if "-f" in cmd else VIDEO)

    monkeypatch.setattr(audio, "run_bounded_process", runner)
    failed, hit = audio.cached_or_probe_audio_file(str(path))
    assert hit is False
    assert failed["transient_probe_failure"] is True
    assert "FileNotFoundError" in failed["probe_error"]
    assert not saved

    # A genuine bad MP3 decode (nonzero subprocess return) is still definitive.
    monkeypatch.setattr(audio, "run_bounded_process", lambda cmd, **kw: (
        BoundedProcessResult(1, b"", b"bad audio") if cmd[0] == "ffmpeg"
        else _response(MP3 if "-f" in cmd else VIDEO)
    ))
    permanently_bad, _ = audio.cached_or_probe_audio_file(str(path))
    assert permanently_bad.get("transient_probe_failure") is not True
    assert permanently_bad["mp3_fallback_checked"] is True
    assert permanently_bad["audio_stream_count"] == 0
    assert len(saved) == 1


def test_inconclusive_audiobook_evidence_does_not_claim_unsafe(tmp_path, monkeypatch):
    import app.audiobook_evidence as evidence_module

    path = _id3_mp3(tmp_path)
    monkeypatch.setattr(evidence_module, "inspect_media_path", lambda _path: {
        "candidateCount": 1, "counts": {"unknown": 1},
        "items": [{"path": str(path), "kind": "unknown", "detectedFormat": "vvc"}],
    })
    monkeypatch.setattr(evidence_module, "verify_audio_files", lambda paths: (
        audio.summarize_audiobook_verification([{
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "probe_error": "MP3 recovery could not complete (TimeoutExpired)",
            "transient_probe_failure": True,
        }])
    ))
    monkeypatch.setattr(evidence_module, "analyze_audio_identity_set", lambda *args: {})
    monkeypatch.setattr(evidence_module, "_filename_identity_support", lambda *args: {})
    monkeypatch.setattr(evidence_module, "representative_items", lambda probes, count: [])
    monkeypatch.setattr(evidence_module, "classify_audio", lambda *args: (
        "REVIEW", 40, "NO_TAGS", []
    ))
    monkeypatch.setattr(evidence_module, "audio_metadata_summary", lambda *args: {})
    result = evidence_module.build_audiobook_evidence(
        {"title": "A Book", "author": "A Writer"}, str(path.parent)
    )
    assert result["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert result["confidence"] == 0
    assert result["source"] == "audiobook-technical-inconclusive"
    assert result["evidence"]["audiobookProbeInconclusive"] is True
    assert result["evidence"]["technical"]["transient_file_count"] == 1


def test_transient_saved_audiobook_verification_retries_but_history_is_readable(monkeypatch):
    row = {
        "id": 5, "source": "audiobook-technical-inconclusive",
        "verdict": "INSUFFICIENT_EVIDENCE",
        "evidence_json": json.dumps({
            "audiobookProbeInconclusive": True,
            "audiobookProbeRecoveryVersion": 1,
            "technical": {"reason_code": "AUDIO_PROBE_TRANSIENT_FAILURE"},
        }),
    }
    class Conn:
        def execute(self, *args):
            return self
        def fetchone(self):
            return row

    @contextmanager
    def fake_conn():
        yield Conn()

    monkeypatch.setattr(verifier, "init_verification_db", lambda: None)
    monkeypatch.setattr(verifier, "local_conn", fake_conn)
    monkeypatch.setattr(verifier, "_verification_signature", lambda *args: "sig")
    result = {"format": "audiobook"}
    assert verifier._verification_for_fingerprint(result, "path", "fingerprint") is None
    historical = verifier._verification_for_fingerprint(
        result, "path", "fingerprint", allow_transient_retry=False
    )
    assert historical["verdict"] == "INSUFFICIENT_EVIDENCE"


def test_real_generated_id3_mp3_recovery_when_ffmpeg_available(tmp_path, monkeypatch):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe not installed")
    path = tmp_path / "short.mp3"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=mono", "-t", "0.3",
         "-c:a", "libmp3lame", "-id3v2_version", "4", "-y", str(path)],
        check=True, capture_output=True, timeout=30,
    )
    assert path.read_bytes()[:3] == b"ID3"
    original_runner = audio.run_bounded_process

    def fake_misdetection(cmd, **kwargs):
        if cmd[0] == "ffprobe" and "-f" not in cmd:
            return _response(VIDEO)
        return original_runner(cmd, **kwargs)

    monkeypatch.setattr(audio, "run_bounded_process", fake_misdetection)
    result = audio.probe_audio_file(str(path))
    assert result["audio_stream_count"] == 1
    assert result["codec"] == "mp3"
    assert result["mp3_fallback_verified"] is True
