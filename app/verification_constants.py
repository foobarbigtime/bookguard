"""Shared limits used by ebook extraction and identity classification."""

FRONT_TEXT_CHARS = 160_000
MIN_USEFUL_TEXT = 500
# A PDF is read by OCR only when its own text layer is this empty. A text layer with real words in it
# (even a few) is better evidence than OCR of the same page, and OCR is held to a stricter rule.
OCR_ONLY_BELOW_CHARS = 20
# The longest title or author kept from a file's own metadata.
MAX_METADATA_CHARS = 1_000
# Bumped when what OCR reads (pages, ISBNs) changes, so scans left undecided by an older version are looked at once more.
OCR_VERSION = 2
