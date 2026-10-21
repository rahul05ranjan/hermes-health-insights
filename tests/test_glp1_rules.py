"""GLP-1 / incretin monitoring module: opt-in, personal-baseline rules, informational wording, synthetic data only."""
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import pytest
from pathlib import Path

from health_insights import modules, settings
from health_insights.concern_rules import glp1_rules

CHI = settings.timezone()
REF = date(2030, 6, 30)


def _iso(d, hm="08:00"):
    h, m = map(int, hm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=CHI).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path):
        self.path = str(path)
        self.con = sqlite3.connect(self.path)
        self.con.execute("CREATE TABLE samples (sample_id INTEGER PRIMARY KEY, source_id TEXT, type_code TEXT, client_record_id TEXT, start_time TEXT, end_time TEXT, value REAL, unit TEXT, metadata_json TEXT)")
        self.con.execute("CREATE TABLE workouts (workout_id INTEGER PRIMARY KEY, source_id INTEGER, client_record_id TEXT, workout_type TEXT, start_time TEXT, end_time TEXT, duration_seconds INTEGER, energy_kcal REAL, distance_meters REAL)")
        self.n = 0

    def add(self, code, day, value, unit="", hm="08:00"):
        self.n += 1
        ts = _iso(day, hm)
        self.con.execute("INSERT INTO samples (source_id,type_code,client_record_id,start_time,end_time,value,unit) VALUES ('t',?,?,?,?,?,?)",
                         (code, f"r{self.n}", ts, ts, value, unit))

    def series(self, code, start_offset, end_offset, fn, unit="", hm="08:00"):
        for off in range(start_offset, end_offset + 1):
            v = fn(off)
            if v is not None:
                self.add(code, REF + timedelta(days=off), v, unit, hm)

    def workout(self, day, kind, minutes=45):
        self.n += 1
        ts = _iso(day, "18:00")
        self.con.execute("INSERT INTO workouts (source_id,client_record_id,workout_type,start_time,end_time,duration_seconds) VALUES (1,?,?,?,?,?)",
                         (f"w{self.n}", kind, ts, ts, minutes * 60))

    def done(self):
        self.con.commit()
        self.con.close()
        return self.path


@pytest.fixture
def opts(monkeypatch, tmp_path):
    monkeypatch.setenv("HEALTH_INSIGHTS_MODULES_FILE", str(tmp_path / "modules.yaml"))
    monkeypatch.setenv("HEALTH_INSIGHTS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("HEALTH_INSIGHTS_MODULES", "glp1")
    settings.reset()
    yield
    settings.reset()


def _ids(findings):
    return {f.id for f in findings}


def _all(db):
    return glp1_rules.glp1_findings(db, REF)


def test_module_is_registered_and_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HEALTH_INSIGHTS_MODULES_FILE", str(tmp_path / "m.yaml"))
    monkeypatch.setenv("HEALTH_INSIGHTS_CONFIG", str(tmp_path / "c.yaml"))
    monkeypatch.delenv("HEALTH_INSIGHTS_MODULES", raising=False)
    settings.reset()
    assert "glp1" in modules.REGISTRY
    assert modules.is_enabled("glp1") is False
    db = DB(tmp_path / "d.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 70 if o < -6 else 84)
    assert glp1_rules.glp1_findings(db.done(), REF) == []


def test_empty_database_gives_no_findings_and_no_crash(opts, tmp_path):
    assert _all(DB(tmp_path / "d.sqlite").done()) == []


def test_rhr_rise_discuss_level(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 62 if o < -6 else 74)
    f = [x for x in _all(db.done()) if x.id == "glp1_rhr_rise"]
    assert f and f[0].level == 2 and "12 bpm" in f[0].evidence


def test_rhr_rise_info_level_for_a_smaller_rise(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 62 if o < -6 else 68)
    f = [x for x in _all(db.done()) if x.id == "glp1_rhr_rise"]
    assert f and f[0].level == 1


def test_rhr_stable_and_short_history_are_quiet(opts, tmp_path):
    db = DB(tmp_path / "a.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 62)
    assert "glp1_rhr_rise" not in _ids(_all(db.done()))
    db = DB(tmp_path / "b.sqlite")
    db.series("resting_heart_rate", -9, 0, lambda o: 90)
    assert _all(db.done()) == []


def test_bp_drop(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("blood_pressure_systolic", -34, -7, lambda o: 130, "mmHg")
    db.series("blood_pressure_diastolic", -34, -7, lambda o: 82, "mmHg")
    db.series("blood_pressure_systolic", -5, 0, lambda o: 108, "mmHg")
    db.series("blood_pressure_diastolic", -5, 0, lambda o: 70, "mmHg")
    f = [x for x in _all(db.done()) if x.id == "glp1_bp_lower"]
    assert f and f[0].level == 1 and "22 mmHg" in f[0].evidence


def test_weight_loss_rate_flag_and_quiet_case(opts, tmp_path):
    db = DB(tmp_path / "a.sqlite")
    db.series("weight", -34, 0, lambda o: 100.0 - max(0, o + 34) * 0.3, "kg")  # about 10 kg in 34 days
    f = [x for x in _all(db.done()) if x.id == "glp1_weight_rate"]
    assert f and f[0].level == 1 and "per week" in f[0].evidence
    db = DB(tmp_path / "b.sqlite")
    db.series("weight", -34, 0, lambda o: 100.0 - max(0, o + 34) * 0.05, "kg")
    assert "glp1_weight_rate" not in _ids(_all(db.done()))


def test_lb_weights_are_converted(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, 0, lambda o: (100.0 - max(0, o + 34) * 0.3) * 2.20462, "lb")
    assert "glp1_weight_rate" in _ids(_all(db.done()))


def test_protein_low_uses_grams_per_kg(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -10, 0, lambda o: 100.0, "kg")
    db.series("dietary_energy_consumed", -13, 0, lambda o: 1800, "kcal", "12:00")
    db.series("dietary_protein", -13, 0, lambda o: 70, "g", "12:00")  # 0.7 g/kg
    f = [x for x in _all(db.done()) if x.id == "glp1_protein_low"]
    assert f and f[0].level == 1 and "0.7 g/kg" in f[0].evidence


def test_protein_adequate_and_ckd_option_are_quiet(opts, tmp_path, monkeypatch):
    db = DB(tmp_path / "a.sqlite")
    db.series("weight", -10, 0, lambda o: 100.0, "kg")
    db.series("dietary_energy_consumed", -13, 0, lambda o: 2200, "kcal", "12:00")
    db.series("dietary_protein", -13, 0, lambda o: 140, "g", "12:00")
    assert "glp1_protein_low" not in _ids(_all(db.done()))
    (tmp_path / "modules.yaml").write_text("glp1:\n  enabled: true\n  options: {ckd: true}\n")
    monkeypatch.delenv("HEALTH_INSIGHTS_MODULES")
    settings.reset()
    db = DB(tmp_path / "b.sqlite")
    db.series("weight", -10, 0, lambda o: 100.0, "kg")
    db.series("dietary_energy_consumed", -13, 0, lambda o: 1800, "kcal", "12:00")
    db.series("dietary_protein", -13, 0, lambda o: 40, "g", "12:00")
    assert "glp1_protein_low" not in _ids(_all(db.done()))


def test_protein_needs_enough_logged_days(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -10, 0, lambda o: 100.0, "kg")
    db.series("dietary_energy_consumed", -2, 0, lambda o: 1800, "kcal", "12:00")
    db.series("dietary_protein", -2, 0, lambda o: 30, "g", "12:00")
    assert "glp1_protein_low" not in _ids(_all(db.done()))


def test_energy_very_low_days(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("dietary_energy_consumed", -6, 0, lambda o: 700, "kcal", "12:00")
    f = [x for x in _all(db.done()) if x.id == "glp1_energy_low"]
    assert f and f[0].level == 1 and "7 of 7" in f[0].evidence


def test_dehydration_pattern_needs_three_signals(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -9, -3, lambda o: 100.0, "kg")
    db.series("weight", -2, 0, lambda o: 97.0, "kg")                       # 3% drop
    db.series("resting_heart_rate", -34, -3, lambda o: 62)
    db.series("resting_heart_rate", -2, 0, lambda o: 72)                   # +10
    db.series("dietary_energy_consumed", -2, 0, lambda o: 600, "kcal", "12:00")
    f = [x for x in _all(db.done()) if x.id == "glp1_dehydration_pattern"]
    assert f and f[0].level == 2
    assert "vomiting" in f[0].advice.lower()


def test_dehydration_pattern_quiet_with_only_weight_change(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -9, -3, lambda o: 100.0, "kg")
    db.series("weight", -2, 0, lambda o: 97.0, "kg")
    assert "glp1_dehydration_pattern" not in _ids(_all(db.done()))


def test_lean_share_flag(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, -28, lambda o: 100.0, "kg")
    db.series("weight", -6, 0, lambda o: 96.0, "kg")
    db.series("lean_body_mass", -34, -28, lambda o: 70.0, "kg")
    db.series("lean_body_mass", -6, 0, lambda o: 67.0, "kg")               # 3 of 4 kg
    f = [x for x in _all(db.done()) if x.id == "glp1_lean_share"]
    assert f and f[0].level == 1 and "75%" in f[0].evidence


def test_strength_training_gap_while_losing_weight(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, -28, lambda o: 100.0, "kg")
    db.series("weight", -6, 0, lambda o: 96.0, "kg")
    for off in (-20, -10, -3):
        db.workout(REF + timedelta(days=off), "running")
    f = [x for x in _all(db.done()) if x.id == "glp1_strength_gap"]
    assert f and f[0].level == 1


def test_strength_training_present_is_quiet(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, -28, lambda o: 100.0, "kg")
    db.series("weight", -6, 0, lambda o: 96.0, "kg")
    for off in range(-27, 0, 3):
        db.workout(REF + timedelta(days=off), "traditional_strength_training")
    assert "glp1_strength_gap" not in _ids(_all(db.done()))


def test_glucose_lows(opts, tmp_path):
    db = DB(tmp_path / "a.sqlite")
    for off, v in ((-3, 66), (-2, 68), (-1, 65)):
        db.add("blood_glucose", REF + timedelta(days=off), v, "mg/dL")
    f = [x for x in _all(db.done()) if x.id == "glp1_glucose_low"]
    assert f and f[0].level == 2
    db = DB(tmp_path / "b.sqlite")
    db.add("blood_glucose", REF, 50, "mg/dL")
    f = [x for x in _all(db.done()) if x.id == "glp1_glucose_low"]
    assert f and f[0].level == 2 and "54" in f[0].evidence
    db = DB(tmp_path / "c.sqlite")
    db.add("blood_glucose", REF, 92, "mg/dL")
    assert "glp1_glucose_low" not in _ids(_all(db.done()))


def test_all_finding_text_is_free_of_dosing_advice(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 62 if o < -6 else 76)
    db.series("weight", -34, 0, lambda o: 100.0 - max(0, o + 34) * 0.3, "kg")
    db.series("dietary_energy_consumed", -13, 0, lambda o: 700, "kcal", "12:00")
    db.series("dietary_protein", -13, 0, lambda o: 40, "g", "12:00")
    db.add("blood_glucose", REF, 50, "mg/dL")
    findings = _all(db.done())
    assert len(findings) >= 4
    for f in findings:
        assert modules.lint_advice(" ".join([f.title, f.evidence, f.advice])) == [], f.id


def test_enabled_module_feeds_the_concerns_engine(opts, tmp_path):
    from health_insights import concerns
    db = DB(tmp_path / "d.sqlite")
    db.series("resting_heart_rate", -34, 0, lambda o: 62 if o < -6 else 76)
    found = concerns.evaluate(db.done(), REF.isoformat())
    assert "glp1_rhr_rise" in {f.id for f in found}


def test_report_lists_measurements_red_flags_and_gaps(opts, tmp_path):
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, 0, lambda o: 100.0 - max(0, o + 34) * 0.1, "kg")
    text = "\n".join(glp1_rules.glp1_report(db.done(), REF))
    assert "GLP-1" in text
    assert "Weight" in text
    assert "not enough" in text.lower()          # data gaps are stated, not hidden
    assert "seek urgent" in text.lower() or "urgent" in text.lower()
    assert "pancreatitis" in text.lower() or "abdominal pain" in text.lower()
    assert modules.lint_advice(text) == []


def test_cli_report_needs_the_module_on_and_prints_flags(tmp_path, monkeypatch, capsys):
    from health_insights import cli
    monkeypatch.setenv("HEALTH_INSIGHTS_MODULES_FILE", str(tmp_path / "m.yaml"))
    monkeypatch.setenv("HEALTH_INSIGHTS_CONFIG", str(tmp_path / "c.yaml"))
    monkeypatch.delenv("HEALTH_INSIGHTS_MODULES", raising=False)
    settings.reset()
    db = DB(tmp_path / "d.sqlite")
    db.series("weight", -34, 0, lambda o: 100.0, "kg")
    path = db.done()
    assert cli.main(["modules", "report", "glp1", "--db", path, "--date", REF.isoformat()]) == 2
    assert cli.main(["modules", "enable", "glp1"]) == 0
    capsys.readouterr()
    assert cli.main(["modules", "report", "glp1", "--db", path, "--date", REF.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "988" in out and "Bring these to your clinician" in out
    assert modules.lint_advice(out) == []
    assert cli.main(["modules", "info", "glp1"]) == 0
    assert "sudden loss of vision" in capsys.readouterr().out.lower()
    settings.reset()


def test_a_documented_option_key_is_actually_read(opts, monkeypatch):
    """The docs say `weight_pct_per_week`; the code must read the same key (#3)."""
    Path(os.environ["HEALTH_INSIGHTS_MODULES_FILE"]).write_text(
        "glp1:\n  enabled: true\n  options: {weight_pct_per_week: 5.0}\n")
    monkeypatch.delenv("HEALTH_INSIGHTS_MODULES")  # the env list would mask the file's mapping
    settings.reset()
    assert glp1_rules._opts()["weight_pct_per_week"] == 5.0


def test_an_unknown_option_key_is_named_not_swallowed(opts, capsys, monkeypatch):
    """A typo'd key warns and leaves the default, instead of silently doing nothing (#3)."""
    Path(os.environ["HEALTH_INSIGHTS_MODULES_FILE"]).write_text(
        "glp1:\n  enabled: true\n"
        "  options: {weight_loss_pct_per_week: 5.0, ckd: true}\n")
    monkeypatch.delenv("HEALTH_INSIGHTS_MODULES")  # the env list would mask the file's mapping
    settings.reset()
    out = glp1_rules._opts()
    err = capsys.readouterr().err
    assert out["weight_pct_per_week"] == 1.5  # the default stands: the key was never read
    assert out["ckd"] is True  # known keys still apply
    assert "weight_loss_pct_per_week" in err
    assert "weight_pct_per_week" in err  # the warning names the known keys too
