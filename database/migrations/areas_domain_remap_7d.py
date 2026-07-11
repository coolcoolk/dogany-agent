#!/usr/bin/env python3
"""
Reclassify areas table domain column to locked 7-domain taxonomy.
Idempotent: safe to run multiple times.
DGN-257.
"""

import sqlite3
import sys
from pathlib import Path

# Mapping: area name -> new domain
MAPPING = {
    "신체건강": "건강",
    "정신건강": "건강",
    "식습관": "건강",
    "스킨케어/위생": "건강",
    "부모님/가족": "관계",
    "친구": "관계",
    "연인": "관계",
    "고양이": "관계",
    "회사/커리어": "커리어",
    "사업/부업": "커리어",
    "소비/저축": "재정",
    "투자": "재정",
    "자기계발": "성장",
    "내 작업 관리": "성장",
    "집 & 생활": "집",
    "가사노동": "집",
    "여행": "여가",
    "문화생활": "여가",
    "취미": "여가",
    "패션": "여가",
}

# Expected area names (for validation)
EXPECTED_NAMES = set(MAPPING.keys())

DB_PATH = Path(__file__).resolve().parent.parent / "lifekit.db"

def main():
    """Apply the reclassification in a single transaction."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        # Validate: fetch all current area names
        cursor.execute("SELECT id, name, domain FROM areas ORDER BY id")
        rows = cursor.fetchall()

        if len(rows) != 20:
            print(f"FATAL: Expected 20 areas, found {len(rows)}", file=sys.stderr)
            sys.exit(1)

        current_names = {row[1] for row in rows}

        # Check for missing or extra names
        missing = EXPECTED_NAMES - current_names
        extra = current_names - EXPECTED_NAMES

        if missing:
            print(f"FATAL: Missing areas in table: {missing}", file=sys.stderr)
            sys.exit(1)
        if extra:
            print(f"FATAL: Unexpected areas in table: {extra}", file=sys.stderr)
            sys.exit(1)

        # Build and execute transaction
        changes = []
        with conn:
            for area_id, name, old_domain in rows:
                new_domain = MAPPING[name]
                if old_domain != new_domain:
                    cursor.execute(
                        "UPDATE areas SET domain = ? WHERE id = ?",
                        (new_domain, area_id)
                    )
                    changes.append((area_id, name, old_domain, new_domain))
                    print(f"  id={area_id} {name}: {old_domain} -> {new_domain}")

        if changes:
            print(f"\nSummary: {len(changes)} areas reclassified")
        else:
            print("Summary: 0 changes (already in target taxonomy)")

        conn.close()
        return 0

    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
