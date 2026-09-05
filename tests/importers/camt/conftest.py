"""CAMT specific test helpers: the fixture files in ``tests/fixtures/camt``."""

import pathlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "camt"


def load_fixture(name):
    return (FIXTURES / name).read_bytes()


def uploaded_fixture(name):
    return SimpleUploadedFile(name, load_fixture(name), content_type="application/xml")


@pytest.fixture
def camt_file():
    """Factory: ``camt_file("batch_booking.xml")`` → uploadable fixture file."""
    return uploaded_fixture
