"""
Command-line interface for the health_insights library.

Usage:
    python -m health_insights.cli coverage [db_path] [--window 30]
    python -m health_insights.cli daily    [db_path] <type_code> [--window 30]
    python -m health_insights.cli baseline [db_path] <type_code> [--window 30]
    python -m health_insights.cli bp [db_path] --date YYYY-MM-DD

The receiver database is optional everywhere: --db (or the positional path), then the
HEALTH_INSIGHTS_BRIDGE_DB environment variable, then `bridge_db` in the config file, then the first
line of ~/.config/healthrelay/db-path.

`coverage` prints the full coverage/freshness report (JSON + Markdown).
`daily` prints the daily aggregate table for one type.
`baseline` prints the 30/90-day baseline for one type.

All output is local; no values are sent anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import analysis
from health_insights import __version__, settings
from health_insights.sqlite_ro import connect_readonly


class _DbError(Exception):
    """The receiver database could not be resolved or opened; main() prints it and exits 2."""


_LABS_DB_HELP = "Path of the labs SQLite database (default: <data dir>/labs/labs.sqlite)"
_DB_HELP = "Path to the receiver SQLite database (default: HEALTH_INSIGHTS_BRIDGE_DB, bridge_db in the config, or ~/.config/healthrelay/db-path)"


def _db_problem(path: str) -> str | None:
    """Why *path* cannot be opened read-only as a SQLite database, or None when it can."""
    import os
    import sqlite3

    if not os.path.exists(path):
        return "no such file"
    if os.path.isdir(path):
        return "it is a directory"
    if os.path.getsize(path) == 0:
        return "it is a 0-byte file, not a database"
    try:
        conn = connect_readonly(path)
        try:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        return str(exc) or type(exc).__name__
    return None


def _receiver_db(explicit=None) -> str:
    """Resolve (--db, env, config, db-path file) and check the receiver database; raise _DbError with the one-line message."""
    path = explicit or settings.bridge_db()
    tail = (f"Set it with --db, HEALTH_INSIGHTS_BRIDGE_DB, bridge_db in {settings.config_path()}, "
            "or ~/.config/healthrelay/db-path.")
    if not path:
        raise _DbError(f"health-insights: cannot open the receiver database at (not set): no database configured. {tail}")
    problem = _db_problem(path)
    if problem is not None:
        raise _DbError(f"health-insights: cannot open the receiver database at {path}: {problem}. {tail}")
    return path


def _add_db_option(parser, required_help=_DB_HELP):
    parser.add_argument("--db", help=required_help)


def build_parser():
    """Build the CLI parser (kept apart from _parse_args so tests can pin its contract)."""
    parser = argparse.ArgumentParser(
        prog="health-insights",
        description="Health monitoring analysis (stdlib).",
    )
    parser.add_argument("--version", action="version", version=f"health-insights {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_cov = sub.add_parser("coverage", help="Full coverage/freshness report")
    p_cov.add_argument("db_path", nargs="?", help=_DB_HELP)
    _add_db_option(p_cov, "Same as the positional path")
    p_cov.add_argument("--window", type=int, default=analysis.DEFAULT_METRIC_WINDOW_DAYS)
    p_cov.add_argument("--baseline", type=int, default=analysis.DEFAULT_BASELINE_WINDOW_DAYS)
    p_cov.add_argument("--json", action="store_true", help="Print JSON document")
    p_cov.add_argument("--md", action="store_true", help="Print Markdown report")
    p_cov.add_argument("--out", help="Write JSON to file")

    p_daily = sub.add_parser("daily", help="Daily aggregates for one type")
    p_daily.add_argument("first", nargs="?", metavar="[db_path] type_code", help="`daily <db_path> <type_code>` or `daily <type_code>` (database resolved as for --db)")
    p_daily.add_argument("second", nargs="?", metavar=argparse.SUPPRESS, help=argparse.SUPPRESS)
    _add_db_option(p_daily)
    p_daily.add_argument("--window", type=int, default=analysis.DEFAULT_METRIC_WINDOW_DAYS)
    p_daily.add_argument("--baseline", type=int, default=analysis.DEFAULT_BASELINE_WINDOW_DAYS)

    p_base = sub.add_parser("baseline", help="30/90-day baseline for one type")
    p_base.add_argument("first", nargs="?", metavar="[db_path] type_code", help="`baseline <db_path> <type_code>` or `baseline <type_code>` (database resolved as for --db)")
    p_base.add_argument("second", nargs="?", metavar=argparse.SUPPRESS, help=argparse.SUPPRESS)
    _add_db_option(p_base)
    p_base.add_argument("--window", type=int, default=analysis.DEFAULT_METRIC_WINDOW_DAYS)
    p_base.add_argument("--baseline", type=int, default=analysis.DEFAULT_BASELINE_WINDOW_DAYS)

    p_anom = sub.add_parser("anomalies", help="Anomaly flags for one local day")
    p_anom.add_argument("db_path", nargs="?", help=_DB_HELP)
    _add_db_option(p_anom, "Same as the positional path")
    p_anom.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to yesterday in your configured time zone")
    p_anom.add_argument("--config", help="Path to health_monitor.yaml")

    p_bp = sub.add_parser("bp", help="Blood pressure bands and flags for one local day")
    p_bp.add_argument("db_path", nargs="?", help=_DB_HELP)
    _add_db_option(p_bp, "Same as the positional path")
    p_bp.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to today in your configured time zone")

    p_doc = sub.add_parser("doctor", help="Check the setup: config, database paths, data freshness (exit 0 ok, 1 warning, 2 error)")
    p_doc.add_argument("--db", help=_DB_HELP)
    p_doc.add_argument("--stale-days", type=int, help="Warn when the newest data is older than this many days (default 3, or stale_days in the config)")
    p_doc.add_argument("--json", action="store_true", help="Print JSON document")

    p_demo = sub.add_parser("demo", help="Write a synthetic demo database (no real data) to try the commands")
    p_demo.add_argument("--out", required=True, help="Path of the SQLite file to create")
    p_demo.add_argument("--days", type=int, default=60)

    p_mod = sub.add_parser("modules", help="Optional modules you can turn on or off (all off by default)")
    mod_sub = p_mod.add_subparsers(dest="modules_command", required=True)
    p_ml = mod_sub.add_parser("list", help="Show every module and whether it is on")
    p_ml.add_argument("--json", action="store_true")
    for name, text in (("enable", "Turn a module on"), ("disable", "Turn a module off")):
        p_mx = mod_sub.add_parser(name, help=text)
        p_mx.add_argument("module_id")
    p_mr = mod_sub.add_parser("report", help="A readable report from a module that has one (for example glp1)")
    p_mr.add_argument("module_id")
    p_mr.add_argument("--db", help=_DB_HELP)
    p_mr.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to yesterday")
    p_mi = mod_sub.add_parser("info", help="What a module does, what data it needs, and its cautions")
    p_mi.add_argument("module_id")
    p_mi.add_argument("--db", help="Also check which of its data types exist in this database (given explicitly only)")

    p_wk = sub.add_parser("weekly", help="Weekly health trends: 7d vs 28d per metric + BP")
    p_wk.add_argument("--db", help=_DB_HELP)
    p_wk.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to today in your configured time zone")
    p_wk.add_argument("--config", help="Path to health_monitor.yaml")

    # labs: nested subcommand group for the clinical-records importer.
    p_labs = sub.add_parser("labs", help="Lab results importer/summary (clinical-records export)")
    labs_sub = p_labs.add_subparsers(dest="labs_command", required=True)

    p_imp = labs_sub.add_parser("import", help="Import a clinical-records export")
    p_imp.add_argument("zip_path", help="Path to export_YYYY-MM-DD.zip")
    p_imp.add_argument("--db", help=_LABS_DB_HELP)

    p_sum = labs_sub.add_parser("summary", help="Summary counts from a DB")
    p_sum.add_argument("--db", help=_LABS_DB_HELP)

    p_trd = labs_sub.add_parser("trend", help="Trend for one LOINC code")
    p_trd.add_argument("--db", help=_LABS_DB_HELP)
    p_trd.add_argument("--loinc", required=True, help="LOINC code")

    p_dig = labs_sub.add_parser("digest-line", help="New-results digest line for --date")
    p_dig.add_argument("--db", help=_LABS_DB_HELP)
    p_dig.add_argument("--date", help="Local day (YYYY-MM-DD)")

    p_json = labs_sub.add_parser("import-json", help="Import lab results from a FHIR JSON file")
    p_json.add_argument("json_path", help="Path to JSON file (list of Observations or FHIR Bundle)")
    p_json.add_argument("--db", help=_LABS_DB_HELP)

    p_labs_bridge = labs_sub.add_parser("import-bridge", help="Import lab results from the bridge DB")
    p_labs_bridge.add_argument("--bridge", help=_DB_HELP)
    p_labs_bridge.add_argument("--db", help=_LABS_DB_HELP)

    # ecg: Apple Watch ECG importer/summary/digest-line (electrocardiograms/ export members).
    p_ecg = sub.add_parser("ecg", help="Apple Watch ECG importer/summary (electrocardiograms export)")
    ecg_sub = p_ecg.add_subparsers(dest="ecg_command", required=True)

    p_imp = ecg_sub.add_parser("import", help="Import an electrocardiograms export")
    p_imp.add_argument("--zip", required=True, help="Path to export_YYYY-MM-DD.zip")
    p_imp.add_argument("--db", required=True, help="SQLite DB path")

    p_json = ecg_sub.add_parser("import-json", help="Import ECG recordings from JSON")
    p_json.add_argument("json_path", help="Path to JSON file")
    p_json.add_argument("--db", required=True, help="SQLite DB path")

    p_sum = ecg_sub.add_parser("summary", help="Summary counts from a DB")
    p_sum.add_argument("--db", required=True, help="SQLite DB path")

    p_dig = ecg_sub.add_parser("digest-line", help="New-results digest line for --date")
    p_dig.add_argument("--db", required=True, help="SQLite DB path")
    p_dig.add_argument("--date", help="Local day (YYYY-MM-DD)")

    p_bridge = ecg_sub.add_parser("import-bridge", help="Import ECG recordings from the bridge DB")
    p_bridge.add_argument("--bridge", help=_DB_HELP)
    p_bridge.add_argument("--db", required=True, help="SQLite DB path")

    # nutrition: food-log intake vs DRI
    p_nut = sub.add_parser("nutrition", help="Nutrition report: daily intake vs DRI")
    p_nut.add_argument("--db", help=_DB_HELP)
    p_nut.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to today in your configured time zone")
    p_nut.add_argument("--days", type=int, default=7, help="Window size in days")
    p_nut.add_argument("--dri", default=str(settings.package_data("dri.yaml")), help="Path to a DRI table (default: the shipped adult-male 31-50 table)")
    p_nut.add_argument("--labs", help="Path to labs.sqlite for cross-check")
    p_nut.add_argument("--json", action="store_true", help="Print JSON document")

    # compounds: factual compound exposure summary
    p_cmp = sub.add_parser("compounds", help="Factual compound exposure summary")
    p_cmp.add_argument("--db", help=_DB_HELP)
    p_cmp.add_argument("--from", dest="from_date", help="Start local day (YYYY-MM-DD)")
    p_cmp.add_argument("--to", dest="to_date", help="End local day (YYYY-MM-DD)")
    p_cmp.add_argument("--json", action="store_true", help="Print JSON document")

    # concerns: health concern findings
    p_con = sub.add_parser("concerns", help="Health concern findings")
    p_con.add_argument("--db", help=_DB_HELP)
    p_con.add_argument("--date", help="Local day (YYYY-MM-DD); defaults to today in your configured time zone")
    p_con.add_argument("--json", action="store_true", help="Print JSON list of findings")

    # workouts: weekly summary and daily digest lines
    p_wrt = sub.add_parser("workouts", help="Workout summary and digest lines")
    wrt_sub = p_wrt.add_subparsers(dest="workouts_command", required=True)

    p_wl = wrt_sub.add_parser("weekly-line", help="Weekly workout summary line for --date")
    p_wl.add_argument("--db", help=_DB_HELP)
    p_wl.add_argument("--date", required=True, help="Reference day (YYYY-MM-DD)")

    p_dl = wrt_sub.add_parser("digest-line", help="Digest line for --date")
    p_dl.add_argument("--db", help=_DB_HELP)
    p_dl.add_argument("--date", required=True, help="Day (YYYY-MM-DD)")

    return parser


def _parse_args(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ("daily", "baseline"):
        # `<cmd> <db_path> <type_code>` (original form) or `<cmd> <type_code>` (database resolved).
        if args.second is not None:
            db_path, type_code = args.first, args.second
        else:
            db_path, type_code = None, args.first
        if type_code is None:
            parser.error("the following arguments are required: type_code")
        args.db_path = args.db or db_path
        args.type_code = type_code
    elif args.command in ("coverage", "anomalies", "bp"):
        args.db_path = args.db or args.db_path
    return args


def _load_report(db_path, window, baseline):
    return analysis.build_report(_receiver_db(db_path), window_days=window, baseline_days=baseline)


def cmd_coverage(args) -> int:
    report = _load_report(args.db_path, args.window, args.baseline)
    if args.json or (not args.md):
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            print(f"Wrote JSON report to {args.out}")
        else:
            print(json.dumps(report, indent=2))
    if args.md:
        print()
        print(analysis.markdown_report(report))
    return 0


def cmd_daily(args) -> int:
    report = _load_report(args.db_path, args.window, args.baseline)
    metric = report["metrics"].get(args.type_code)
    if not metric:
        print(f"No data for type '{args.type_code}'", file=sys.stderr)
        return 1
    print(json.dumps(metric["daily"], indent=2))
    return 0


def cmd_baseline(args) -> int:
    report = _load_report(args.db_path, args.window, args.baseline)
    metric = report["metrics"].get(args.type_code)
    if not metric:
        print(f"No data for type '{args.type_code}'", file=sys.stderr)
        return 1
    print(json.dumps(metric["baseline"], indent=2))
    return 0


def _default_date() -> str:
    """Yesterday in America/Chicago (the digest/anomaly day)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    return (datetime.now(settings.timezone()) - timedelta(days=1)).strftime("%Y-%m-%d")


def cmd_anomalies(args) -> int:
    """Print anomaly flags for one local day (counts/bounds only)."""
    import yaml
    from . import anomalies

    date = args.date or _default_date()
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    else:
        config = {
            "timezone": settings.timezone_name(),
            "metrics": {
                "resting_heart_rate": {"baseline_window_days": 30, "anomaly": {"robust_sigma": 3.0, "min_abs": 45, "max_abs": 120}, "trend": {"slope_per_day_alert": 0.5}},
                "heart_rate": {"baseline_window_days": 30, "anomaly": {"robust_sigma": 3.0, "min_abs": 30, "max_abs": 220}, "trend": {"slope_per_day_alert": 0.5}},
                "heart_rate_variability_sdnn": {"baseline_window_days": 30, "anomaly": {"robust_sigma": 3.0, "min_abs": 5, "max_abs": 250}, "trend": {"slope_per_day_alert": 2.0}},
                "weight": {"baseline_window_days": 30, "anomaly": {"robust_sigma": 3.0, "min_abs": 30, "max_abs": 400, "max_change_over_days": 2, "window_days": 3}, "trend": {"slope_per_day_alert": 0.2}},
                "sleep_analysis": {"baseline_window_days": 30, "anomaly": {"robust_sigma": 3.0, "min_abs": 0, "max_abs": 24}, "trend": {"slope_per_day_alert": 1.0}},
            },
        }

    result = anomalies.detect(_receiver_db(args.db_path), config, date)
    flags: list[str] = []
    insufficient: list[str] = []
    for tc in sorted(result):
        entry = result[tc]
        if entry["status"] == "flagged":
            flags.append(f"{tc}:{entry['reason']}")
        elif entry["status"] == "insufficient":
            insufficient.append(tc)

    for line in flags:
        print(line)
    if flags and insufficient:
        print()
    if insufficient:
        print(f"insufficient history: {', '.join(insufficient)}")
    if not flags and not insufficient:
        print("no flags")
    return 0


def cmd_bp(args) -> int:
    """Print BP latest/7d-avg/flags for one Chicago day."""
    from . import bp

    date = args.date or _default_date()
    for line in bp.report(_receiver_db(args.db_path), date):
        print(line)
    return 0


def cmd_weekly(args) -> int:
    """Print the weekly health trends report (7d vs 28d per metric + BP)."""
    import yaml
    from . import weekly

    date = args.date or _default_date()
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    else:
        # Default: load the shipped health_monitor.yaml from the package data.
        import os
        default_cfg = str(settings.package_data("health_monitor.yaml"))
        if os.path.exists(default_cfg):
            with open(default_cfg, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {"timezone": settings.timezone_name(), "metrics": {}}
    for line in weekly.report(_receiver_db(args.db), config, date):
        print(line)
    return 0


# ---- labs: clinical-records importer --------------------------------------

def _labs_dispatch(args) -> int:
    """Route `labs <subcommand>` to the labs module handlers."""
    from . import labs

    sub = getattr(args, "labs_command")
    labs_db = args.db or str(settings.labs_dir() / "labs.sqlite")
    if sub == "import":
        return labs.cmd_import(args.zip_path, labs_db)
    if sub == "summary":
        return labs.cmd_summary(labs_db)
    if sub == "trend":
        return labs.cmd_trend(labs_db, args.loinc)
    if sub == "digest-line":
        return labs.cmd_digest_line(labs_db, args.date)
    if sub == "import-json":
        return labs.cmd_import_json(args.json_path, labs_db)
    if sub == "import-bridge":
        return labs.cmd_import_bridge(_receiver_db(args.bridge), labs_db)
    return 1


def _ecg_dispatch(args) -> int:
    """Route `ecg <subcommand>` to the ecg module handlers."""
    from . import ecg

    sub = getattr(args, "ecg_command")
    if sub == "import":
        return ecg.cmd_import(args.zip, args.db)
    if sub == "import-json":
        return ecg.cmd_import_json(args.json_path, args.db)
    if sub == "summary":
        return ecg.cmd_summary(args.db)
    if sub == "digest-line":
        return ecg.cmd_digest_line(args.db, args.date)
    if sub == "import-bridge":
        return ecg.cmd_import_bridge(_receiver_db(args.bridge), args.db)
    return 1


def _nutrition_dispatch(args) -> int:
    """Route `nutrition` to the nutrition module."""
    from . import nutrition

    date = args.date or _default_date()
    db = _receiver_db(args.db)
    if args.json:
        j = nutrition.report_json(
            db, args.dri, date, days=args.days, labs_db=args.labs
        )
        print(json.dumps(j, indent=2))
    else:
        for line in nutrition.report(
            db, args.dri, date, days=args.days, labs_db=args.labs
        ):
            print(line)
    return 0


def _workouts_dispatch(args) -> int:
    """Route `workouts <subcommand>` to the workouts module."""
    from . import workouts

    sub = getattr(args, "workouts_command")
    if sub in ("weekly-line", "digest-line"):
        db = _receiver_db(args.db)
    if sub == "weekly-line":
        line = workouts.weekly_line(db, args.date)
        if line:
            print(line)
        return 0
    if sub == "digest-line":
        line = workouts.digest_line(db, args.date)
        if line:
            print(line)
        return 0
    return 1


def _modules_dispatch(args) -> int:
    from . import modules

    cmd = args.modules_command
    if cmd == "list":
        rows = [{"id": m.id, "title": m.title, "category": m.category, "summary": m.summary,
                 "enabled": modules.is_enabled(m.id)} for m in sorted(modules.REGISTRY.values(), key=lambda m: m.id)]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return 0
        width = max((len(r["id"]) for r in rows), default=2)
        for r in rows:
            print(f"[{'on ' if r['enabled'] else 'off'}] {r['id']:<{width}}  {r['title']}: {r['summary']}")
        print(f"\nTurn one on:  health-insights modules enable <id>     Details: health-insights modules info <id>")
        print(f"Switches are stored in {settings.modules_file()}")
        return 0
    try:
        module = modules.get(args.module_id)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    if cmd in ("enable", "disable"):
        path = modules.set_enabled(module.id, cmd == "enable")
        print(f"{module.title} is now {'ON' if cmd == 'enable' else 'OFF'} (saved in {path}).")
        if cmd == "enable":
            for caution in module.cautions:
                print(f"  Note: {caution}")
        return 0
    if cmd == "report":
        if module.report is None:
            print(f"{module.title} has no report; it only adds checks to `concerns`.", file=sys.stderr)
            return 2
        if not modules.is_enabled(module.id):
            print(f"{module.title} is off. Turn it on first: health-insights modules enable {module.id}", file=sys.stderr)
            return 2
        print("\n".join(module.report(_receiver_db(args.db), args.date or _default_date())))
        return 0
    info_db = _receiver_db(args.db) if getattr(args, "db", None) else None
    print(f"{module.title} ({module.id}) - {'ON' if modules.is_enabled(module.id) else 'off'}")
    print(f"  {module.summary}")
    print(f"  Category: {module.category}")
    if module.data_needs:
        print(f"  Works best with: {', '.join(module.data_needs)}")
    if module.skills:
        print(f"  Agent skills: {', '.join(module.skills)}")
    for caution in module.cautions:
        print(f"  Caution: {caution}")
    if module.red_flags:
        print("  Act on these whatever the data says (urgent or emergency care):")
        for flag in module.red_flags:
            print(f"    - {flag}")
    if info_db:
        info = modules.readiness(module.id, info_db)
        print(f"  In {info_db}: have {', '.join(info['present']) or 'none'}; missing {', '.join(info['missing']) or 'none'}")
    return 0


def _concerns_dispatch(args) -> int:
    """Route `concerns` to the concerns module."""
    from . import concerns

    date = args.date or _default_date()
    results = concerns.evaluate(_receiver_db(args.db), date)

    if args.json:
        print(json.dumps([f.to_dict() for f in results], indent=2, ensure_ascii=False))
    else:
        if not results:
            print("✅ No concerns found")
        else:
            for f in results:
                print(concerns.format_line(f))
    return 0


def _compounds_dispatch(args) -> int:
    """Route `compounds` to the evidence module."""
    from . import evidence
    from .intake_reader import read_effective_components

    db = _receiver_db(args.db)
    conn = connect_readonly(db)

    try:
        components = read_effective_components(conn)
    except Exception:
        components = []
    finally:
        conn.close()

    rows = evidence.compound_exposure_summary(
        components,
        start=getattr(args, "from_date", None),
        end=getattr(args, "to_date", None),
    )

    if args.json:
        doc = {"compounds": []}
        for r in rows:
            doc["compounds"].append({
                "substance": r.substance,
                "form": r.form,
                "unit": r.unit,
                "total": str(r.total) if r.total is not None else None,
                "known_count": r.known_count,
                "unknown_count": r.unknown_count,
                "first_day": r.first_day,
                "last_day": r.last_day,
                "days": r.days,
                "sources": r.sources,
            })
        print(json.dumps(doc, indent=2))
    else:
        if not rows:
            print("No compounds logged")
            return 0
        # Print a small aligned table
        header = f"{'Substance':<20} {'Form':<25} {'Unit':<8} {'Total':>10} {'Known':>7} {'Unknown':>9} {'Days':>6} {'First':>11} {'Last':>11} {'Sources'}"
        print(header)
        print("-" * len(header))
        for r in rows:
            total_str = str(r.total) if r.total is not None else "N/A"
            sources_str = ", ".join(r.sources)
            print(
                f"{r.substance:<20} {str(r.form or ''):<25} {str(r.unit or ''):<8} "
                f"{total_str:>10} {r.known_count:>7} {r.unknown_count:>9} "
                f"{r.days:>6} {r.first_day:>11} {r.last_day:>11} {sources_str}"
            )
    return 0


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except _DbError as exc:
        print(exc, file=sys.stderr)
        return 2


def _run(args) -> int:
    if args.command == "demo":
        from . import demo
        demo.build(args.out, days=args.days)
        print(f"Wrote synthetic demo database to {args.out}. Try: health-insights weekly --db {args.out}")
        return 0
    if args.command == "modules":
        return _modules_dispatch(args)
    if args.command == "doctor":
        from . import doctor
        return doctor.run(args.db, args.stale_days, args.json)
    if args.command == "coverage":
        return cmd_coverage(args)
    if args.command == "daily":
        return cmd_daily(args)
    if args.command == "baseline":
        return cmd_baseline(args)
    if args.command == "anomalies":
        return cmd_anomalies(args)
    if args.command == "bp":
        return cmd_bp(args)
    if args.command == "weekly":
        return cmd_weekly(args)
    if args.command == "labs":
        return _labs_dispatch(args)
    if args.command == "ecg":
        return _ecg_dispatch(args)
    if args.command == "nutrition":
        return _nutrition_dispatch(args)
    if args.command == "workouts":
        return _workouts_dispatch(args)
    if args.command == "concerns":
        return _concerns_dispatch(args)
    if args.command == "compounds":
        return _compounds_dispatch(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
