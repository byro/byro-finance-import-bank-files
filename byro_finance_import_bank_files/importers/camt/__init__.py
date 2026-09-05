"""CAMT.053 importer.

``parser`` is the pure CAMT.053 parser (no Django, no byro); ``importer`` is
the thin adapter to byro's bank transaction importer API. This package must not
import ``importer`` here so that the parser stays importable without Django.
"""
