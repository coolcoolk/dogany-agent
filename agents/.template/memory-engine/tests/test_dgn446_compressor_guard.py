# test-lane: lib compatibility import for the component suite named below
"""Keep the deployed pytest/direct entry point; the component suite owns cases."""
from test_compressor_guard import *  # noqa: F401,F403 -- legacy pytest collection
import test_compressor_guard as _owner

if __name__ == "__main__":
    raise SystemExit(_owner.pytest.main([_owner.__file__, "-q", "-rfEs"]))
