import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from byro.common.models.configuration import Configuration


@pytest.fixture
def configuration():
    config = Configuration.get_solo()
    config.name = "Association Name"
    config.backoffice_mail = "associationname@example.com"
    config.mail_from = "associationname@example.com"
    config.save()
    return config


@pytest.fixture
def user():
    user = get_user_model().objects.create(username="regular_user", is_staff=True)
    user.set_password("test_password")
    user.save()
    yield user
    user.delete()


@pytest.fixture
def logged_in_client(client, user):
    client.post(
        reverse("common:login"),
        {"username": user.username, "password": "test_password"},
    )
    return client
