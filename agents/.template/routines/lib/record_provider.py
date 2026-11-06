"""Optional installed record adapter, selected explicitly by its owner.

The framework never searches a kit directory or names a developer module.
An unset provider leaves general records independent of ticket lifecycle.
"""
import importlib.util
import os
from pathlib import Path


def configured():
    path = os.environ.get('DOGANY_RECORD_PROVIDER', '').strip()
    if not path:
        return None
    target = Path(path)
    if not target.is_absolute() or not target.is_file():
        raise ValueError('record provider must be an existing absolute file')
    spec = importlib.util.spec_from_file_location('_dogany_record_provider', target)
    provider = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provider)
    return provider
