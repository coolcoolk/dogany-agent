#!/usr/bin/env bash
# test-lane: lib Install explicit quota facts and policy dependencies in scratch dispatch workspaces.
# Lifecycle fixtures stub usage-gate.sh; their detached workers also need a
# measured weekly pace. Never weaken the production unknown-usage refusal.
# Pinned return routing is resolved before admission in isolated dispatchers.
set -eu
fixture_workspace="$1"
routines_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$fixture_workspace/routines/lib"
for policy_file in dispatch_routing.py runtime_policy.py runtime-traits.tsv return_route.py; do
  cp "$routines_source/lib/$policy_file" "$fixture_workspace/routines/lib/"
done
if [[ ! -e "$fixture_workspace/routines/usage-gate.py" ]]; then
  cat > "$fixture_workspace/routines/usage-gate.py" <<'PY'
from datetime import datetime, timedelta, timezone

def get_usage():
    now = datetime.now(timezone.utc)
    return dict(five_hour_util=10, seven_day_util=10,
                five_hour_resets_at=(now + timedelta(hours=2)).isoformat(),
                seven_day_resets_at=(now + timedelta(days=2)).isoformat())

def usage_history():
    return []
PY
fi
# Positive lifecycle fixtures must satisfy the real new-work Why door.
# Install its dependencies and pin the fixture's explicit Why before parsing
# the case-specific flags; test-stage-replay owns the negative intake cases.
cp "$routines_source/stage-judge.py" "$fixture_workspace/routines/"
# Preserve each fixture's deliberate missing/broken optional dependencies.
# The stage adapter needs these modules, not the entire runtime library.
for stage_file in stage_judge.py base_contract.py problem_cards.py \
                  review_evidence.py ticket_receipts.py routine_use.py update_defer.py conf_reader.py; do
  cp "$routines_source/lib/$stage_file" "$fixture_workspace/routines/lib/"
done
mkdir -p "$fixture_workspace/routines/lib/why_net"
cp "$routines_source/lib/why_net/"*.py "$fixture_workspace/routines/lib/why_net/"
python3 "$routines_source/tests/fixtures/stage-why.py" "$fixture_workspace"
python3 - "$fixture_workspace/routines/dispatch-detached.sh" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
if path.exists():
    path.write_text(path.read_text().replace('WHY_ID=""', 'WHY_ID="why-9999"', 1))
PY
