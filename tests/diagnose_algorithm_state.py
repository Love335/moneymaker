"""
diagnose_algorithm_state.py — Check that all parts of the bot agree
on which algorithm is currently active.

Reads every file and data source that records or influences algorithm
selection, and reports any disagreements.

Run with:
    cd ~/moneymaker
    sudo ~/moneymaker/venv/bin/python3 tests/diagnose_algorithm_state.py
"""

import sys
import os
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

OK   = "\033[92m[✓]\033[0m"
WARN = "\033[93m[!]\033[0m"
FAIL = "\033[91m[✗]\033[0m"
INFO = "\033[94m[i]\033[0m"

findings = []

def report(status, source, value, note=""):
    symbol = {"ok": OK, "warn": WARN, "fail": FAIL, "info": INFO}[status]
    note_str = f"  ← {note}" if note else ""
    print(f"  {symbol} {source:<35} {value}{note_str}")
    findings.append((status, source, value))

def section(title):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ── 1. settings.json ──────────────────────────────────────────

section("1. Persistent Settings (data/settings.json)")

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "settings.json"

if not SETTINGS_PATH.exists():
    report("fail", "settings.json", "FILE NOT FOUND")
    settings_algo = None
else:
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
        settings_algo = settings.get("active_algorithm", "NOT SET")
        settings_mode = settings.get("trading_mode", "NOT SET")
        report("info", "settings.json / active_algorithm", settings_algo)
        report("info", "settings.json / trading_mode",     settings_mode)
    except Exception as exc:
        report("fail", "settings.json", f"PARSE ERROR: {exc}")
        settings_algo = None


# ── 2. StateManager default ───────────────────────────────────

section("2. StateManager Default (state.py AppState)")

try:
    from core.state import AppState, VALID_ALGORITHMS
    default_algo = AppState().active_algorithm
    report("info", "AppState default active_algorithm", default_algo)
    report("info", "VALID_ALGORITHMS", str(sorted(VALID_ALGORITHMS)))

    if settings_algo and settings_algo not in VALID_ALGORITHMS:
        report("fail", "settings.json algo validity",
               settings_algo, "NOT in VALID_ALGORITHMS — will be rejected")
    elif settings_algo:
        report("ok", "settings.json algo validity",
               settings_algo, "is a valid algorithm name")
except Exception as exc:
    report("fail", "state.py import", str(exc))


# ── 3. ALGORITHM_REGISTRY in engine.py ───────────────────────

section("3. Engine Algorithm Registry (engine.py)")

try:
    from core.engine import ALGORITHM_REGISTRY
    report("info", "ALGORITHM_REGISTRY keys", str(sorted(ALGORITHM_REGISTRY.keys())))

    for name, cls in ALGORITHM_REGISTRY.items():
        instance = cls()
        matches  = instance.name == name
        status   = "ok" if matches else "fail"
        report(
            status,
            f"  {name}",
            f"class={cls.__name__}, instance.name='{instance.name}'",
            "" if matches else "MISMATCH — class.name != registry key"
        )

    if settings_algo and settings_algo not in ALGORITHM_REGISTRY:
        report("fail", "settings algo in ALGORITHM_REGISTRY",
               settings_algo, "NOT FOUND — engine cannot load this algorithm")
    elif settings_algo:
        report("ok", "settings algo in ALGORITHM_REGISTRY",
               settings_algo, "engine can load it")
except Exception as exc:
    report("fail", "engine.py import", str(exc))


# ── 4. next_algorithm() cycle order ──────────────────────────

section("4. Menu Cycle Order (StateManager.next_algorithm)")

try:
    from core.state import StateManager, VALID_ALGORITHMS
    sm = StateManager()

    # Show the full cycle starting from each algorithm
    print()
    print("  Full cycle sequence (alphabetical — this is what the menu cycles through):")
    algorithms = sorted(VALID_ALGORITHMS)
    for i, algo in enumerate(algorithms):
        next_algo = algorithms[(i + 1) % len(algorithms)]
        print(f"    {algo:<20} → {next_algo}")

    print()
    print("  If you pressed the button N times from dual_momentum:")
    current = "dual_momentum"
    for n in range(1, len(algorithms) + 2):
        idx     = algorithms.index(current)
        current = algorithms[(idx + 1) % len(algorithms)]
        marker  = " ← settings say this" if current == settings_algo else ""
        print(f"    {n} press(es): {current}{marker}")
except Exception as exc:
    report("fail", "StateManager import", str(exc))


# ── 5. Settings module default values ────────────────────────

section("5. Settings Module Defaults (data/settings.py)")

try:
    from data.settings import DEFAULTS
    default_algo_in_settings = DEFAULTS.get("active_algorithm", "NOT DEFINED")
    report("info", "DEFAULTS['active_algorithm']", default_algo_in_settings)

    if default_algo_in_settings != AppState().active_algorithm:
        report("warn", "Default mismatch",
               f"settings.py default='{default_algo_in_settings}' "
               f"vs AppState default='{AppState().active_algorithm}'",
               "these should match")
    else:
        report("ok", "Defaults consistent",
               f"both default to '{default_algo_in_settings}'")
except Exception as exc:
    report("fail", "data/settings.py import", str(exc))


# ── 6. Algorithm instances agree on their own names ───────────

section("6. Algorithm Self-Identity (each algorithm's .name property)")

try:
    from algorithms.dual_momentum  import DualMomentumAlgorithm
    from algorithms.mean_reversion import MeanReversionAlgorithm
    from algorithms.trend_following import TrendFollowingAlgorithm

    for cls, expected_name in [
        (DualMomentumAlgorithm,  "dual_momentum"),
        (MeanReversionAlgorithm, "mean_reversion"),
        (TrendFollowingAlgorithm,"trend_following"),
    ]:
        instance = cls()
        matches  = instance.name == expected_name
        report(
            "ok" if matches else "fail",
            f"{cls.__name__}.name",
            f"'{instance.name}'",
            "" if matches else f"expected '{expected_name}'"
        )
        report("info", f"{cls.__name__}.evaluation_interval_seconds",
               f"{instance.evaluation_interval_seconds}s "
               f"({'hourly' if instance.evaluation_interval_seconds == 3600 else 'daily' if instance.evaluation_interval_seconds == 86400 else 'other'})")
except Exception as exc:
    report("fail", "algorithm imports", str(exc))


# ── 7. Mean reversion universe ────────────────────────────────

section("7. MeanReversion Universe (confirms tickers.py is loaded correctly)")

try:
    from algorithms.mean_reversion import UNIVERSE
    report("info", "MeanReversion UNIVERSE size", f"{len(UNIVERSE)} stocks")
    for ticker in sorted(UNIVERSE):
        print(f"    {ticker}")
except Exception as exc:
    report("fail", "mean_reversion UNIVERSE", str(exc))


# ── 8. Simulate what main.py does on startup ──────────────────

section("8. Startup Simulation (what main.py would load right now)")

try:
    from data.settings import Settings
    s = Settings()

    loaded_algo = s.get("active_algorithm")
    loaded_mode = s.get("trading_mode")

    report("info", "Settings().get('active_algorithm')", loaded_algo)
    report("info", "Settings().get('trading_mode')",     loaded_mode)

    # Simulate engine.start(starting_algo=loaded_algo)
    from core.engine import ALGORITHM_REGISTRY
    if loaded_algo in ALGORITHM_REGISTRY:
        cls      = ALGORITHM_REGISTRY[loaded_algo]
        instance = cls()
        report("ok", "Would instantiate", f"{cls.__name__} (instance.name='{instance.name}')")
        report(
            "ok" if instance.name == loaded_algo else "fail",
            "instance.name matches settings",
            f"'{instance.name}' == '{loaded_algo}'",
            "" if instance.name == loaded_algo else "MISMATCH"
        )
        report("info", "Evaluation interval",
               f"Every {instance.evaluation_interval_seconds}s "
               f"({'hourly' if instance.evaluation_interval_seconds == 3600 else 'daily'})")
    else:
        report("fail", "Algorithm not in registry", loaded_algo)
except Exception as exc:
    report("fail", "Startup simulation", str(exc))


# ── 9. Check for duplicate Settings instances risk ────────────

section("9. Settings Instance Check")

try:
    from data.settings import Settings, SETTINGS_FILE
    report("info", "Settings file path", str(SETTINGS_FILE))
    report("info", "Settings file exists", str(SETTINGS_FILE.exists()))

    s1 = Settings()
    s2 = Settings()
    s1_algo = s1.get("active_algorithm")
    s2_algo = s2.get("active_algorithm")

    if s1_algo == s2_algo:
        report("ok", "Two Settings() instances agree",
               f"both read '{s1_algo}'")
    else:
        report("fail", "Two Settings() instances DISAGREE",
               f"'{s1_algo}' vs '{s2_algo}'",
               "file is being modified between reads")
except Exception as exc:
    report("fail", "Settings instance check", str(exc))


# ── Summary ───────────────────────────────────────────────────

print()
print("=" * 60)
print("  Summary")
print("=" * 60)

fails = [f for f in findings if f[0] == "fail"]
warns = [f for f in findings if f[0] == "warn"]

if not fails and not warns:
    print(f"\n  {OK} All checks passed — every component agrees.")
else:
    if fails:
        print(f"\n  {FAIL} {len(fails)} failure(s):")
        for _, source, value in fails:
            print(f"       {source}: {value}")
    if warns:
        print(f"\n  {WARN} {len(warns)} warning(s):")
        for _, source, value in warns:
            print(f"       {source}: {value}")

print()
print("  Note: this script reads files and imports modules only.")
print("  It does not connect to Avanza or modify any state.")
print()