from pathlib import Path

import app.pdf_probe as pdf_probe


def _minimal_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.7\n%%EOF\n")


def test_metadata_probe_rejects_oversized_worker_output(monkeypatch):
    pdf_probe._probe_cached.cache_clear()

    def oversized(*args, **kwargs):
        raise pdf_probe.ProcessOutputLimitExceeded("stdout", pdf_probe.MAX_WORKER_OUTPUT_BYTES)

    monkeypatch.setattr(pdf_probe, "run_bounded_process", oversized)

    result = pdf_probe._probe_cached("/tmp/fake.pdf", 1, 1, 1, 1, 1)

    assert result["error"] == "PDF metadata probe returned too much output."


def test_integrity_probe_rejects_oversized_worker_output(tmp_path, monkeypatch):
    path = tmp_path / "book.pdf"
    _minimal_pdf(path)

    def oversized(*args, **kwargs):
        raise pdf_probe.ProcessOutputLimitExceeded("stdout", pdf_probe.MAX_WORKER_OUTPUT_BYTES)

    monkeypatch.setattr(pdf_probe, "run_bounded_process", oversized)

    result = pdf_probe.inspect_pdf_integrity(path)

    assert result["error"] == "PDF integrity probe returned too much output."


def test_content_probe_rejects_oversized_worker_output(tmp_path, monkeypatch):
    path = tmp_path / "book.pdf"
    _minimal_pdf(path)

    def oversized(*args, **kwargs):
        raise pdf_probe.ProcessOutputLimitExceeded("stdout", 1024)

    monkeypatch.setattr(pdf_probe, "run_bounded_process", oversized)

    result = pdf_probe.extract_pdf_text(
        path,
        max_chars=1000,
        page_limit=1,
        front_chars=500,
    )

    assert result["error"] == "PDF content extraction returned too much output."
