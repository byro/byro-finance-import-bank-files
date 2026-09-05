"""Plugin level tests: one Django app, one entry point, n registered importers."""

from django.apps import apps

from byro.bookkeeping.bank_import import get_bank_transaction_importers
from byro.common.utils import get_plugins
from byro_finance_import_bank_files import signals
from byro_finance_import_bank_files.importers.camt.importer import Camt053Importer

APP_LABEL = "byro_finance_import_bank_files"
CAMT053 = "byro_finance_import_bank_files.camt053"


def test_app_config_with_plugin_meta_is_loaded():
    app = apps.get_app_config(APP_LABEL)
    assert type(app).__name__ == "PluginApp"
    assert hasattr(app, "ByroPluginMeta")
    assert str(app.ByroPluginMeta.name) == "Bank file importers"


def test_plugin_is_listed_by_byro():
    assert APP_LABEL in [app.label for app in get_plugins()]


def test_importer_is_registered_with_byro():
    importers = get_bank_transaction_importers()
    assert isinstance(importers[CAMT053], Camt053Importer)
    assert str(importers[CAMT053].label) == "CAMT.053 bank statement"


def test_every_plugin_importer_is_registered_under_its_identifier():
    registered = get_bank_transaction_importers()
    assert [type(importer) for importer in signals.IMPORTERS] == [Camt053Importer]
    for importer in signals.IMPORTERS:
        assert registered[importer.identifier] is importer
