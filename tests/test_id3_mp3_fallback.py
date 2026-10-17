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
    path.write_bytes(b"ID3\\x04\\x00\\x00\\x00\\x00\\x00\\x00" + b"x" * 5000)
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
