"""Framework-owned artifact encoding for installed kit manifests."""
import hashlib
import json
from pathlib import Path

from bridge.session_state import Refusal, contained


ARTIFACT_DIGEST_CONTRACT = "artifact-digest/1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


class PinnedArtifactCodec:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._receipt()

    def _receipt(self):
        packs = self.root / "config/packs"
        receipt = packs / "kit_life.receipt"
        if (packs / "kit_life.receipt.inprogress").exists():
            raise Refusal(33, "kit installation is incomplete")
        try:
            rows = receipt.read_text().splitlines()
            for required in ("schema=1", "pack=kit_life", "class=kit", "status=delivery-complete"):
                key = required.split("=", 1)[0] + "="
                if [line for line in rows if line.startswith(key)] != [required]:
                    raise Refusal(33, "kit installation receipt is incomplete")
        except (OSError, Refusal) as exc:
            raise Refusal(33, "installed artifact codec is unavailable") from exc

    def artifacts(self, root, manifests):
        if Path(root).resolve() != self.root:
            raise Refusal(33, "artifact codec belongs to another instance")
        self._receipt()
        result = {}
        for owner, paths in manifests.items():
            if not paths or len(paths) != len(set(paths)):
                raise Refusal(33, "artifact manifest is empty or repeats a path")
            try:
                measured = {rel: "sha256:" + hashlib.sha256(contained(self.root, rel).read_bytes()).hexdigest()
                            for rel in paths}
            except (OSError, Refusal) as exc:
                raise Refusal(33, "artifact manifest path is unavailable") from exc
            result[owner] = digest(measured)
        return result
