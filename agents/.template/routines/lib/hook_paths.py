"""Instance-owned hook state must not follow a session's working directory."""
import os


def instance_root():
    """Use the enrolled Codex root or this installed library's instance.

    Payload cwd can be a sealed service directory, another checkout, or an
    unrelated directory. Even an existing .claude there is not an instance
    marker: earlier hooks may have left it behind.
    """
    enrolled = os.environ.get('DOGANY_CODEX_ADAPTER_ROOT')
    if enrolled:
        return os.path.abspath(enrolled)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
