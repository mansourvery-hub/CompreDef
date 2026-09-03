"""
parser.py - Compatibility layer for legacy tests and imports.
Redirects to provider.py, renderer.py and utils.py.
"""
# Dual-context sibling imports (relative inside Anki's package load,
# absolute in the top-level test harness — see core.py for why).
if __package__:
    from .provider import LocalSQLiteProvider, IndexingError
    from .renderer import render_structured_content_node, render_yomitan_definition_html
    from .utils import (
        is_zip_dictionary,
        is_directory_dictionary,
        extract_clean_word,
        extract_base_text,
        parse_furigana_field,
        find_dictionary_folders,
    )
    from .core import get_provider
else:
    from provider import LocalSQLiteProvider, IndexingError
    from renderer import render_structured_content_node, render_yomitan_definition_html
    from utils import (
        is_zip_dictionary,
        is_directory_dictionary,
        extract_clean_word,
        extract_base_text,
        parse_furigana_field,
        find_dictionary_folders,
    )
    from core import get_provider

RENDERER_VERSION = LocalSQLiteProvider.RENDERER_VERSION

def _get_db_path():
    return get_provider().db_path

def get_single_dictionary(path):
    # Legacy SingleDictionary object mock
    class SingleDictionaryMock:
        def __init__(self, p):
            self.path = p
            self.is_zip = is_zip_dictionary(p)
            self.title = get_provider().get_title(p)
        def is_indexed(self):
            return get_provider().is_installed(self.path)
        def entry_count(self):
            return get_provider().get_entry_count(self.path)
        def install(self, progress_cb=None, cancel_check=None):
            return get_provider().install(self.path, progress_cb, cancel_check)
        def lookup(self, word, reading=""):
            entries = get_provider().lookup_by_path(self.path, word, reading)
            return [e.definition for e in entries]
        def _compute_signature(self):
            return get_provider()._compute_signature(self.path)
        def index_is_current(self):
            return get_provider().is_index_current(self.path)
        def _iter_term_banks(self):
            # For tests that spy on this method
            return get_provider()._iter_term_banks(self.path)

    return SingleDictionaryMock(path)

# Aliasing SingleDictionary to the Mock class for legacy tests
class SingleDictionary:
    @staticmethod
    def _iter_term_banks(self): pass # stub

def install_dictionary(path, progress_cb=None, cancel_check=None):
    return get_provider().install(path, progress_cb, cancel_check)

def uninstall_dictionary(path):
    get_provider().uninstall(path)

def is_dictionary_installed(path):
    return get_provider().is_installed(path)

# Re-export for tests
_loaded_dicts = {} # Added for legacy test compatibility
_extract_base_text = extract_base_text
_INDEX_BATCH_SIZE = LocalSQLiteProvider._INDEX_BATCH_SIZE
# Note: if tests use 'import parser', they get this module.
# If they use 'from parser import ...', they get these functions.
