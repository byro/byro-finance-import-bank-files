from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

from . import __version__


class PluginApp(AppConfig):
    name = "byro_finance_import_bank_files"
    verbose_name = _("Bank file importers")

    class ByroPluginMeta:
        name = _("Bank file importers")
        author = "Nicolas Häuser"
        description = _(
            "Importers for file-based bank statement formats through byro's bank "
            "transaction importer API. Currently supported: CAMT.053."
        )
        visible = True
        version = __version__

    def ready(self):
        try:
            import byro.bookkeeping.bank_import  # noqa: F401
        except ImportError as e:
            raise ImproperlyConfigured(
                "byro-finance-import-bank-files requires a byro version that "
                "provides the bank transaction importer API "
                "(byro.bookkeeping.bank_import, introduced with byro pull "
                "request #514)."
            ) from e
        from . import signals  # noqa: F401
