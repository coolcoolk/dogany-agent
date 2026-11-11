# test-lane: lib shared helper for account resource settings isolation
"""Give each importing suite a private store inherited by its subprocesses."""
import os
import tempfile

# Keep stores alive until process exit, including importlib-loaded fixtures.
_STORES = []


def isolate_resource_settings():
    store = tempfile.TemporaryDirectory(prefix='resource-settings-test-')
    _STORES.append(store)
    os.environ['DOGANY_RESOURCE_SETTINGS_DIR'] = store.name
    return store.name
