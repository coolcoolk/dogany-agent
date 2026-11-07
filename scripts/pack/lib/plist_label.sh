# plist_label <plist> -- print the launchd identity declared by a plist.
# A plist filename is only a path; launchd addresses the value of Label.
plist_label() {
  "${PACK_PY3:-python3}" - "$1" <<'PY'
import plistlib
import sys

path = sys.argv[1]
try:
    with open(path, "rb") as fh:
        label = plistlib.load(fh).get("Label")
except Exception as exc:
    print("cannot read plist Label from %s: %s" % (path, exc), file=sys.stderr)
    raise SystemExit(1)

if not isinstance(label, str) or not label or "\n" in label or "\r" in label:
    print("plist Label is missing or invalid: %s" % path, file=sys.stderr)
    raise SystemExit(1)
print(label)
PY
}
