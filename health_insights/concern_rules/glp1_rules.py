"""GLP-1 / incretin therapy monitoring (opt-in module ``glp1``).

Personal-baseline patterns from data you already collect, worded as information or "worth discussing with your
clinician". Nothing here diagnoses anything, says a medicine caused a change, or gives dosing or medication-change advice.
Thresholds are heuristics (each finding's ``source`` says so) and can be tuned in modules.yaml under
``glp1: {enabled: true, options: {...}}``. Research notes: the GLP-1 Monitoring project folder.

Rules (ids): glp1_rhr_rise, glp1_bp_lower, glp1_weight_rate, glp1_protein_low, glp1_energy_low,
glp1_dehydration_pattern, glp1_lean_share, glp1_strength_gap, glp1_glucose_low.
"""
from __future__ import annotations

import sqlite3
import sys
import statistics
from datetime import date as Date, datetime, timedelta
from typing import Optional

from .. import modules
from health_insights import settings
from health_insights.sqlite_ro import connect_readonly

MODULE_ID = "glp1"
_TZ = settings.timezone()
KG_PER_LB = 1 / 2.20462

DEFAULTS = {
    "rhr_info_bpm": 5,            # 5 of the last 7 days this far above your prior 28-day median: information
    "rhr_discuss_bpm": 10,        # ... and this far: worth discussing
    "rhr_persist_days": 5,
    "bp_drop_mmhg": 15,           # mean of your last 5 readings vs your prior 28-day mean
    "weight_pct_per_week": 1.5,   # heuristic; no evidence-based cut-off exists
    "protein_target_g_per_kg": 1.2,
    "protein_floor_g_per_kg": 0.8,
    "ckd": False,                 # true skips the protein check (higher protein may not suit kidney disease)
    "energy_floor_kcal": 1000,
    "lean_share_pct": 40,
    "lean_noise_kg": 1.5,
    "strength_per_week": 2,
    "glucose_low_mgdl": 70,
    "glucose_very_low_mgdl": 54,
}

RED_FLAGS = (  # act on these whatever the data says (urgent or emergency care)
    "Severe or persistent stomach pain, especially spreading to the back, with or without vomiting (possible pancreatitis).",
    "Severe pain in the upper right belly with fever, chills, yellow skin or eyes, or persistent vomiting (possible gallbladder problem).",
    "Vomiting or diarrhea that will not stop, being unable to keep fluids down, very little urine, fainting or severe dizziness (dehydration and kidney strain).",
    "Swelling of the face, lips or throat, trouble breathing or swallowing, a widespread rash, or a fast heartbeat with faintness (severe allergic reaction).",
    "Shaking, sweating, confusion or blurred vision that does not clear, especially with insulin or sulfonylurea medicines, or any loss of consciousness (low blood sugar).",
    "Sudden loss of vision or a sudden change in vision.",
    "Thoughts of suicide or a mood crisis: call or text 988 in the US, or your local emergency number.",
    "Chest pain, a racing or pounding heartbeat at rest that persists, or fainting.",
)

DISCUSS_FLAGS = (  # bring these to your clinician promptly (not an emergency)
    "A lump or swelling in the neck, hoarseness, or trouble swallowing or breathing that persists.",
    "A resting heart rate that stays well above your usual.",
    "Vision changes if you have diabetes.",
    "A planned or possible pregnancy, or breastfeeding.",
    "An upcoming operation or any procedure under sedation: tell the team about your medicine so they can plan.",
    "Fast or large weight loss together with low food intake, or new weakness or lasting tiredness.",
    "Low mood or unusual changes in mood.",
)

_INFO = "Informational only, not medical advice; this tool cannot tell what is causing a change."


def _opts() -> dict:
    """The module's options over the defaults, with an unknown key named out loud.

    A key outside DEFAULTS is not an error the run should die on, but it must
    never be accepted silently either: these options are user-facing
    configuration, and a typo'd key (the documented-but-unread
    `weight_loss_pct_per_week`, say) leaves the threshold at its default with
    nothing telling the user the setting did not take effect. Unknown keys are
    dropped with a one-line warning on stderr naming them and the known keys.
    """
    merged = dict(DEFAULTS)
    try:
        given = modules.options(MODULE_ID)
    except KeyError:
        return merged
    unknown = sorted(set(given) - set(DEFAULTS))
    if unknown:
        print(f"health-insights: glp1: ignoring unknown option(s): {', '.join(unknown)} "
              f"(known options: {', '.join(sorted(DEFAULTS))})", file=sys.stderr)
    merged.update({k: v for k, v in given.items() if k in DEFAULTS})
    return merged


# ---------------------------------------------------------------------------
# data access (read-only, tolerant of missing tables)
# ---------------------------------------------------------------------------

def _rows(db_path: str, code: str) -> list[tuple[datetime, float, str]]:
    try:
        conn = connect_readonly(db_path)
        cur = conn.execute("SELECT start_time, value, unit FROM samples WHERE type_code = ? AND value IS NOT NULL ORDER BY start_time", (code,))
        raw = cur.fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    out = []
    for ts, value, unit in raw:
        try:
            out.append((datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(_TZ), float(value), (unit or "")))
        except ValueError:
            continue
    return out


def _to_kg(value: float, unit: str) -> float:
    return value * KG_PER_LB if unit.lower().startswith("lb") else value


def _daily(db_path: str, code: str, agg: str = "mean", convert=None) -> dict[Date, float]:
    by_day: dict[Date, list[float]] = {}
    for when, value, unit in _rows(db_path, code):
        by_day.setdefault(when.date(), []).append(convert(value, unit) if convert else value)
    return {d: (sum(v) if agg == "sum" else sum(v) / len(v)) for d, v in by_day.items()}


def _win(series: dict[Date, float], ref: Date, start: int, end: int) -> dict[Date, float]:
    lo, hi = ref + timedelta(days=start), ref + timedelta(days=end)
    return {d: v for d, v in series.items() if lo <= d <= hi}


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def _finding(id_: str, level: int, title: str, evidence: str, advice: str, basis: str):
    from ..concerns import Finding  # late import: concerns imports the module registry
    return Finding(id=id_, level=level, title=title, evidence=evidence, source=f"heuristic ({basis})", advice=advice)


def _weight_kg(db_path: str) -> dict[Date, float]:
    return _daily(db_path, "weight", "mean", _to_kg)


# ---------------------------------------------------------------------------
# individual checks; each returns a list of findings
# ---------------------------------------------------------------------------

def _rhr_series(db_path: str) -> dict[Date, float]:
    return _daily(db_path, "resting_heart_rate")


def _rhr_state(rhr: dict[Date, float], ref: Date, opts: dict):
    recent = _win(rhr, ref, -6, 0)
    base = _win(rhr, ref, -34, -7)
    if len(recent) < 5 or len(base) < 14:
        return None
    return statistics.median(base.values()), recent


def check_rhr(db_path: str, ref: Date, opts: dict) -> list:
    state = _rhr_state(_rhr_series(db_path), ref, opts)
    if state is None:
        return []
    base, recent = state
    persist = int(opts["rhr_persist_days"])
    n_discuss = sum(1 for v in recent.values() if v >= base + opts["rhr_discuss_bpm"])
    n_info = sum(1 for v in recent.values() if v >= base + opts["rhr_info_bpm"])
    delta = round(statistics.median(recent.values()) - base)
    if n_discuss >= persist:
        return [_finding(
            "glp1_rhr_rise", 2, "Resting heart rate has run well above your usual",
            f"{delta} bpm above your prior 28-day median, on {n_discuss} of the last {len(recent)} days",
            "Small average rises are listed in prescribing information for these medicines, so this is a pattern to watch rather than an alarm. "
            "Illness, poor sleep, alcohol, heat and dehydration also push resting heart rate up. Worth mentioning to your clinician if it continues, "
            "and sooner if you have palpitations, dizziness or fainting. " + _INFO,
            "vs your own 28-day baseline; label data: mean +1 to 4 bpm, some people 20+ bpm at a visit")]
    if n_info >= persist:
        return [_finding(
            "glp1_rhr_rise", 1, "Resting heart rate is a little above your usual",
            f"about {delta} bpm above your prior 28-day median, on {n_info} of the last {len(recent)} days",
            "A modest rise like this is common with illness, poor sleep, alcohol or heat, and small average rises are listed in prescribing information "
            "for these medicines. Keep an eye on it. " + _INFO,
            "vs your own 28-day baseline")]
    return []


def _bp_state(db_path: str, ref: Date):
    from .. import bp
    try:
        readings = bp._readings(db_path)
    except Exception:
        return None
    rows = [(Date.fromisoformat(d), s) for d, _t, s, _dia in readings if d <= ref.isoformat()]
    base = [s for d, s in rows if ref - timedelta(days=34) <= d <= ref - timedelta(days=7)]
    last = [s for d, s in rows][-5:]
    if len(base) < 5 or len(last) < 5:
        return None
    return _mean(base), _mean(last), rows


def check_bp(db_path: str, ref: Date, opts: dict) -> list:
    state = _bp_state(db_path, ref)
    if state is None:
        return []
    base, last, _rows_ = state
    drop = round(base - last)
    if drop < opts["bp_drop_mmhg"]:
        return []
    return [_finding(
        "glp1_bp_lower", 1, "Your blood pressure readings are lower than usual",
        f"your last 5 readings average {round(last)} systolic, {drop} mmHg below your usual {round(base)}",
        "Lower readings can go with weight loss, fluid loss or blood pressure treatment. Worth mentioning to your clinician, especially if you feel dizzy or faint; "
        "contact them promptly if you have vomiting or diarrhea, and seek urgent care if you faint. " + _INFO,
        "vs your own readings; label data: hypotension 1.3% vs 0.4%")]


def _weight_change(db_path: str, ref: Date):
    w = _weight_kg(db_path)
    then, now = _win(w, ref, -34, -28), _win(w, ref, -6, 0)
    if len(then) < 2 or len(now) < 2:
        return None
    return _mean(then.values()), _mean(now.values())


def check_weight(db_path: str, ref: Date, opts: dict) -> list:
    change = _weight_change(db_path, ref)
    if change is None:
        return []
    then, now = change
    pct = (then - now) / then * 100
    rate = pct / 4
    if rate < opts["weight_pct_per_week"]:
        return []
    return [_finding(
        "glp1_weight_rate", 1, "Weight is falling quickly",
        f"down {pct:.1f}% over about 4 weeks ({rate:.1f}% per week)",
        "There is no agreed safe rate, and scales move with water. In the research, faster loss goes with more lean-mass loss and gallstones. "
        "Protein, strength training and enough fluids help protect muscle. Worth mentioning to your clinician or a dietitian. " + _INFO,
        f"threshold {opts['weight_pct_per_week']}%/week is a configurable default, not a clinical cut-off")]


def check_protein(db_path: str, ref: Date, opts: dict) -> list:
    if opts.get("ckd"):
        return []
    protein = _win(_daily(db_path, "dietary_protein", "sum"), ref, -13, 0)
    energy = _win(_daily(db_path, "dietary_energy_consumed", "sum"), ref, -13, 0)
    logged = {d: p for d, p in protein.items() if p > 0 and (energy.get(d, 1) > 0)}
    weights = _win(_weight_kg(db_path), ref, -13, 0)
    if len(logged) < 5 or not weights:
        return []
    kg = weights[max(weights)]
    per_kg = _mean(logged.values()) / kg
    if per_kg >= opts["protein_target_g_per_kg"]:
        return []
    below_floor = per_kg < opts["protein_floor_g_per_kg"]
    return [_finding(
        "glp1_protein_low", 1, "Logged protein looks low",
        f"about {per_kg:.1f} g/kg a day ({_mean(logged.values()):.0f} g over {len(logged)} logged days)",
        "A 2025 nutrition advisory suggests 1.2 to 1.6 g/kg a day while losing weight, and the general allowance is 0.8. "
        + ("Yours is below even that. " if below_floor else "")
        + "Food logs are often incomplete, so this may be under-logging. Protein and strength training help protect muscle; a dietitian can set targets. "
        "This check is not for kidney disease (set ckd: true to skip it). " + _INFO,
        "consensus guidance, not trial-validated")]


def check_energy(db_path: str, ref: Date, opts: dict) -> list:
    energy = _win(_daily(db_path, "dietary_energy_consumed", "sum"), ref, -6, 0)
    logged = {d: e for d, e in energy.items() if e > 0}
    if len(logged) < 4:
        return []
    floor = opts["energy_floor_kcal"]
    low = sum(1 for e in logged.values() if e < floor)
    if low < 3:
        return []
    return [_finding(
        "glp1_energy_low", 1, "Very low food intake logged",
        f"{low} of {len(logged)} logged days under {floor} kcal in the last 7 days",
        "Eating very little can leave you short of protein, fluids and vitamins and can cause tiredness or dizziness. It can also just mean the log is incomplete. "
        "Worth mentioning to your clinician or a dietitian if it is real. " + _INFO,
        f"floor {floor} kcal is a configurable default; some people eat under 800 kcal early in treatment")]


def check_dehydration(db_path: str, ref: Date, opts: dict) -> list:
    signals: list[str] = []
    w = _weight_kg(db_path)
    last3, before = _win(w, ref, -2, 0), _win(w, ref, -9, -3)
    if last3 and len(before) >= 3:
        drop_pct = (_mean(before.values()) - _mean(last3.values())) / _mean(before.values()) * 100
        if drop_pct >= 2:
            signals.append(f"weight down {drop_pct:.1f}% in 3 days")
    rhr = _rhr_series(db_path)
    rbase, rlast = _win(rhr, ref, -34, -3), _win(rhr, ref, -2, 0)
    if len(rbase) >= 14 and rlast and _mean(rlast.values()) >= statistics.median(rbase.values()) + opts["rhr_info_bpm"]:
        signals.append("resting heart rate up")
    state = _bp_state(db_path, ref)
    if state is not None:
        base, last, _r = state
        if base - last >= opts["bp_drop_mmhg"]:
            signals.append("blood pressure lower")
    energy = _win(_daily(db_path, "dietary_energy_consumed", "sum"), ref, -2, 0)
    low = [e for e in energy.values() if 0 < e < opts["energy_floor_kcal"]]
    if len(low) >= 2:
        signals.append("very low intake")
    if len(signals) < 3:
        return []
    return [_finding(
        "glp1_dehydration_pattern", 2, "Several changes together that can go with fluid loss",
        "; ".join(signals),
        "This combination can go with dehydration. If you are vomiting, have diarrhea, feel dizzy on standing, or are passing very little urine, "
        "contact your clinician promptly, and seek urgent care if you faint or cannot keep fluids down. Dehydration is the usual route to kidney strain with these medicines. " + _INFO,
        "heuristic composite; labels warn of kidney injury from volume loss")]


def check_lean_share(db_path: str, ref: Date, opts: dict) -> list:
    change = _weight_change(db_path, ref)
    lean = _daily(db_path, "lean_body_mass", "mean", _to_kg)
    then, now = _win(lean, ref, -34, -28), _win(lean, ref, -6, 0)
    if change is None or len(then) < 2 or len(now) < 2:
        return []
    loss = change[0] - change[1]
    lean_loss = _mean(then.values()) - _mean(now.values())
    if loss < 2.0 or lean_loss < opts["lean_noise_kg"]:
        return []
    share = lean_loss / loss * 100
    if share < opts["lean_share_pct"]:
        return []
    return [_finding(
        "glp1_lean_share", 1, "A large share of weight lost looks like lean mass",
        f"about {share:.0f}% of the {loss:.1f} kg lost over 4 weeks is lean mass on your scale",
        "Studies average roughly 25% and range widely. Scales estimate lean mass with electrical current, so hydration, meals and timing shift the number; "
        "compare readings taken under the same conditions. Protein and strength training help protect muscle. Worth mentioning to your clinician or a dietitian. " + _INFO,
        f"trial averages about 25%; threshold {opts['lean_share_pct']}% is a configurable default")]


def _strength_sessions(db_path: str, ref: Date) -> Optional[int]:
    try:
        conn = connect_readonly(db_path)
        rows = conn.execute("SELECT workout_type, end_time FROM workouts").fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    lo = ref - timedelta(days=27)
    count = 0
    for kind, end in rows:
        try:
            day = datetime.fromisoformat(str(end).replace("Z", "+00:00")).astimezone(_TZ).date()
        except ValueError:
            continue
        if lo <= day <= ref and "strength" in str(kind).lower():
            count += 1
    return count


def check_strength(db_path: str, ref: Date, opts: dict) -> list:
    change = _weight_change(db_path, ref)
    if change is None or (change[0] - change[1]) / change[0] * 100 < 2:
        return []
    sessions = _strength_sessions(db_path, ref)
    if sessions is None or sessions / 4 >= opts["strength_per_week"]:
        return []
    return [_finding(
        "glp1_strength_gap", 1, "Little strength training while weight is dropping",
        f"{sessions} strength sessions logged in the last 4 weeks; guidance is at least 3 a week",
        "Strength training is the main way research suggests protecting muscle during weight loss. Only workouts recorded in Apple Health are counted here. "
        "Worth raising with your clinician or a trainer. " + _INFO,
        "consensus guidance (strength at least 3 times a week plus 150 minutes of aerobic activity)")]


def check_glucose(db_path: str, ref: Date, opts: dict) -> list:
    readings = []
    for when, value, unit in _rows(db_path, "blood_glucose"):
        mgdl = value * 18.016 if unit.lower().startswith("mmol") else value
        if ref - timedelta(days=6) <= when.date() <= ref:
            readings.append(mgdl)
    if not readings:
        return []
    low = sum(1 for v in readings if v < opts["glucose_low_mgdl"])
    very_low = sum(1 for v in readings if v < opts["glucose_very_low_mgdl"])
    if not low:
        return []
    level = 2 if (very_low or low >= 3) else 1
    return [_finding(
        "glp1_glucose_low", level, "Low glucose readings",
        f"{low} readings below {opts['glucose_low_mgdl']} mg/dL, {very_low} below {opts['glucose_very_low_mgdl']} mg/dL in the last 7 days",
        "Low readings can be sensor error (for example pressure on a sensor overnight). If you have shakiness, sweating, confusion or fainting, treat it and get help the same day. "
        "Discuss repeated lows with your clinician, especially if you also use insulin or sulfonylurea medicines. " + _INFO,
        "consensus definitions: level 1 is 54 to 69 mg/dL, level 2 is below 54 mg/dL")]


CHECKS = (check_rhr, check_bp, check_weight, check_protein, check_energy, check_dehydration,
          check_lean_share, check_strength, check_glucose)


def glp1_findings(db_path: str, ref_date) -> list:
    """Concern-engine entry point: runs only when the module is enabled, and never raises for missing data."""
    if not modules.is_enabled(MODULE_ID):
        return []
    ref = Date.fromisoformat(ref_date) if isinstance(ref_date, str) else ref_date
    opts = _opts()
    out: list = []
    for check in CHECKS:
        try:
            out.extend(check(db_path, ref, opts))
        except Exception:
            continue  # one bad check must not hide the others
    return out


# ---------------------------------------------------------------------------
# readable report (modules report glp1)
# ---------------------------------------------------------------------------

def glp1_report(db_path: str, ref_date) -> list[str]:
    ref = Date.fromisoformat(ref_date) if isinstance(ref_date, str) else ref_date
    opts = _opts()
    lines = [f"GLP-1 / incretin monitoring, {ref.isoformat()} (patterns in your own data; not medical advice)"]
    gaps: list[str] = []

    w = _weight_kg(db_path)
    change = _weight_change(db_path, ref)
    if change:
        then, now = change
        chg = (now - then) / then * 100
        lines.append(f"Weight: 7-day mean {settings_weight(now)}, {chg:+.1f}% versus about 4 weeks ago ({chg / 4:+.1f}% per week)")
    else:
        gaps.append("weight (needs 2 or more readings in each of the last 7 days and days 28 to 34 ago)")

    protein = _win(_daily(db_path, "dietary_protein", "sum"), ref, -13, 0)
    weights = _win(w, ref, -13, 0)
    logged = {d: p for d, p in protein.items() if p > 0}
    if len(logged) >= 5 and weights:
        lines.append(f"Protein: about {_mean(logged.values()) / weights[max(weights)]:.1f} g/kg a day over {len(logged)} logged days (research range while losing weight: 1.2 to 1.6)")
    else:
        gaps.append("protein (needs 5 or more logged days and a recent weight)")

    state = _rhr_state(_rhr_series(db_path), ref, opts)
    if state:
        base, recent = state
        lines.append(f"Resting heart rate: {statistics.median(recent.values()) - base:+.0f} bpm versus your prior 28-day median ({base:.0f})")
    else:
        gaps.append("resting heart rate (needs 14 baseline days and 5 recent days)")

    bp_state = _bp_state(db_path, ref)
    if bp_state:
        lines.append(f"Blood pressure: last 5 readings average {bp_state[1]:.0f} systolic versus your usual {bp_state[0]:.0f}")
    else:
        gaps.append("blood pressure (needs cuff readings; the watch does not measure it)")

    if not any(_win(_daily(db_path, "blood_glucose", "mean"), ref, -6, 0)):
        gaps.append("glucose (needs a linked sensor or manual entries)")

    findings = glp1_findings(db_path, ref)
    lines.append("")
    if findings:
        lines.append("Worth a look:")
        for f in sorted(findings, key=lambda f: (-f.level, f.id)):
            lines.append(f"- {f.title}: {f.evidence}. {f.advice}")
    else:
        lines.append("Nothing in these checks stood out.")
    if gaps:
        lines.append("")
        lines.append("Not enough data yet for: " + "; ".join(gaps) + ".")
    lines.append("")
    lines.append("Seek urgent or emergency care for any of these, whatever this report says (do not wait for the next check):")
    lines.extend(f"- {flag}" for flag in RED_FLAGS)
    lines.append("")
    lines.append("Bring these to your clinician promptly:")
    lines.extend(f"- {flag}" for flag in DISCUSS_FLAGS)
    lines.append("")
    lines.append("This tool gives no advice about your medication; talk to your prescriber about anything to do with it.")
    return lines


def settings_weight(kg: float) -> str:
    from ..units import to_display_weight, weight_unit
    return f"{to_display_weight(kg):.1f} {weight_unit()}"


modules.register(modules.Module(
    id=MODULE_ID,
    title="GLP-1 / incretin monitoring",
    summary="Watches your own data for patterns worth knowing about while on a GLP-1 type medicine: resting heart rate, blood pressure, weight-loss rate, protein, low intake, fluid loss, lean mass and glucose lows, plus a plain list of warning signs.",
    category="medication",
    data_needs=("weight", "resting_heart_rate", "blood_pressure_systolic", "dietary_protein", "dietary_energy_consumed",
                "lean_body_mass", "blood_glucose"),
    rules=(("glp1", glp1_findings),),
    skills=("health-insights-glp1-monitoring", "health-insights-glp1-nutrition", "health-insights-glp1-warning-signs"),
    cautions=(
        "Informational only, not medical advice. It cannot tell what is causing a change and gives no advice about your medication.",
        "Works from your own baselines, so it needs a few weeks of data and will say when it does not have enough.",
        "Apple Watch does not measure blood pressure or glucose; those need a cuff or a linked sensor. Food logs are often incomplete.",
        "Thresholds are heuristics you can tune in modules.yaml (options); they are not clinical cut-offs.",
    ),
    red_flags=RED_FLAGS,
    report=glp1_report,
))
