"""Signal receivers: register this plugin's bank file importers with byro.

This module is imported from ``PluginApp.ready()`` in ``apps.py``. Every
supported file format is one ``BankTransactionImporter`` in :data:`IMPORTERS`;
the plugin itself stays a single Django app with a single ``byro.plugin``
entry point. Formats are never registered through additional apps or entry
points.
"""

from django.dispatch import receiver

from byro.bookkeeping.signals import bank_transaction_importers

from .importers.camt.importer import Camt053Importer

#: The bank file importers this plugin provides, one per supported format.
#: Importers are stateless, so a single instance each is enough. The signal is
#: sent whenever byro builds the importer selection, so registration must be
#: cheap.
IMPORTERS = [Camt053Importer()]


@receiver(
    bank_transaction_importers,
    dispatch_uid="byro_finance_import_bank_files.bank_file_importers",
)
def register_bank_file_importers(sender, **kwargs):
    return list(IMPORTERS)
