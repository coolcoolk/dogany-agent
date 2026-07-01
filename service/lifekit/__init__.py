"""service.lifekit — stable SDK facade over the lifekit core (DGN-059).

Skills import THIS package, never database/lifekit.py directly, and never the
raw data files (body_stats.json / lifekit.db). Storage details (JSON vs sqlite)
stay hidden behind the core's ConfigStore abstraction, so a future JSON->DB
switch leaves skill call sites unchanged.

Robust import: the lifekit core lives in <repo>/database/lifekit.py, a sibling
of this service/ tree. We resolve that path from this file's absolute location
(service/lifekit/__init__.py -> ../../database) and add it to sys.path before
importing, so the package works regardless of the caller's cwd.
"""

import os
import importlib.util

# <repo>/service/lifekit/__init__.py -> <repo>/database/lifekit.py
_SERVICE_LIFEKIT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_SERVICE_LIFEKIT_DIR))
_CORE_PATH = os.path.join(_REPO_ROOT, 'database', 'lifekit.py')

# Load the core by absolute file path under a distinct module name. This avoids
# the name clash with this package (both would otherwise be 'lifekit') and makes
# the import independent of the caller's cwd / sys.path.
_spec = importlib.util.spec_from_file_location('lifekit_core', _CORE_PATH)
_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_core)

# ── Re-exported stable API ──────────────────────────────────
# Existing core functions (timeseries CRUD, aggregation, body model).
get_conn = _core.get_conn

meal_add = _core.meal_add
meal_find = _core.meal_find
meal_day = _core.meal_day
meal_del = _core.meal_del
meal_upd = _core.meal_upd

workout_add = _core.workout_add
workout_find = _core.workout_find
workout_find_full = _core.workout_find_full
workout_del = _core.workout_del
workout_add_classification = _core.workout_add_classification

agg_day = _core.agg_day
agg_week = _core.agg_week
load_card_data = _core.load_card_data

person_find = _core.person_find
person_add = _core.person_add
person_alias_add = _core.person_alias_add
appt_find = _core.appt_find
appt_add = _core.appt_add
appt_upd = _core.appt_upd
appt_person_add = _core.appt_person_add
appt_persons = _core.appt_persons

# Body stats / target model.
load_body_stats = _core.load_body_stats
compute_targets = _core.compute_targets
compute_macro_goals = _core.compute_macro_goals

# New write API (DGN-059): config store + metric timeseries.
set_stats = _core.set_stats
set_config = _core.set_config
get_config = _core.get_config
log_metric = _core.log_metric
get_series = _core.get_series
latest_metric = _core.latest_metric

# Storage abstraction (exposed for future backend swap / advanced callers).
ConfigStore = _core.ConfigStore
JsonConfigStore = _core.JsonConfigStore

__all__ = [
    'get_conn',
    'meal_add', 'meal_find', 'meal_day', 'meal_del', 'meal_upd',
    'workout_add', 'workout_find', 'workout_find_full', 'workout_del',
    'workout_add_classification',
    'agg_day', 'agg_week', 'load_card_data',
    'person_find', 'person_add', 'person_alias_add',
    'appt_find', 'appt_add', 'appt_upd', 'appt_person_add', 'appt_persons',
    'load_body_stats', 'compute_targets', 'compute_macro_goals',
    'set_stats', 'set_config', 'get_config',
    'log_metric', 'get_series', 'latest_metric',
    'ConfigStore', 'JsonConfigStore',
]
