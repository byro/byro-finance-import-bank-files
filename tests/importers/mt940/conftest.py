"""MT940 specific test helpers: the fixture files in ``tests/fixtures/mt940``."""

import pathlib

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "mt940"


def load_fixture(name):
    return (FIXTURES / name).read_bytes()


def uploaded_fixture(name):
    return SimpleUploadedFile(name, load_fixture(name), content_type="text/plain")


@pytest.fixture
def mt940_file():
    """Factory: ``mt940_file("structured_86.mt940")`` → uploadable fixture file."""
    return uploaded_fixture
