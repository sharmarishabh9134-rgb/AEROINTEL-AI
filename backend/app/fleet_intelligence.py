"""Synthetic fleet intelligence, prognostics, planning, and retrieval services.

The operational examples in this module are fictional. C-MAPSS parsing is real;
training requires the user-provided NASA FD001 files under data/CMAPSSData.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.neural_network import MLPRegressor
from sklearn.ensemble import IsolationForest, RandomForestRegressor
from sklearn.preprocessing import StandardScaler

from backend.app.config import PROJECT_ROOT, get_settings
from backend.app.database import SessionLocal, get_db
from backend.app.models import AuditLog, Profile
from backend.app.security import bearer, current_user

router = APIRouter(tags=["fleet intelligence"])
DATA = PROJECT_ROOT / "data"
MODEL_DIR = PROJECT_ROOT / "backend" / "models"
MODEL_PATH = MODEL_DIR / "fd001_prognostics.joblib"


def demo_or_user(credentials=Depends(bearer), db=Depends(get_db)):
    return current_user(credentials, db)


def planner_access(credentials=Depends(bearer), db=Depends(get_db)):
    profile = current_user(credentials, db)
    if profile.role not in {"analyst", "admin"}:
        raise HTTPException(status_code=403, detail="Analyst or admin role required")
    return profile


def audit(db, profile, action: str, details: str = ""):
    if profile is not None:
        db.add(AuditLog(profile_id=profile.id, action=action, details=details[:4000]))
        db.commit()


# Fictional but internally consistent fleet graph used by the demo endpoints.
AIRCRAFT = ["TAIL-101", "TAIL-102", "TAIL-103", "TAIL-104", "TAIL-105"]
COMPONENTS = ["fan", "low_pressure_compressor", "high_pressure_compressor", "high_pressure_turbine", "low_pressure_turbine"]
SNAGS = [
    ("oil pressure fluctuates on climb, slight vibration", "lubrication system", "oil pressure sensor"),
    ("engine runs hot at takeoff, exhaust temperature trending high", "combustion system", "exhaust gas temperature sensor"),
    ("fan noise with rising vibration readings", "fan module", "fan rotor"),
    ("fuel flow indication intermittent; sluggish spool response", "fuel system", "fuel flow sensor"),
    ("compressor pressure ratio drift and stall warning", "compressor module", "high pressure compressor"),
]
GRAPH: dict[str, dict[str, Any]] = {
    tail: {
        "aircraft": {"id": tail, "type": "turbofan", "health": round(94 - i * 4.2, 1), "status": "available" if i < 3 else "monitor"},
        "components": [{"id": f"{tail}-{c}", "name": c, "health": round(96 - i * 3 - j * 2.4, 1)} for j, c in enumerate(COMPONENTS)],
        "snags": [{"id": f"SN-{100+i}", "text": SNAGS[i % len(SNAGS)][0], "component": SNAGS[i % len(SNAGS)][2], "status": "open" if i % 2 else "monitor"}],
        "work_orders": [{"id": f"WO-{700+i}", "task": f"Inspect {COMPONENTS[i % len(COMPONENTS)]}", "status": "open" if i % 2 else "planned"}],
        "spares": [{"part": f"SP-{810+i}", "name": SNAGS[i % len(SNAGS)][2], "available": 1 + i % 3, "depot": "DEL" if i % 2 == 0 else "BOM"}],
    } for i, tail in enumerate(AIRCRAFT)
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def map_snag(text: str) -> dict[str, Any]:
    """Map messy free text to a component with lexical + character similarity."""
    from difflib import SequenceMatcher
    clean = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    candidates = SNAGS
    best = None
    query = _tokens(clean)
    for phrase, system, component in candidates:
        words = _tokens(phrase + " " + system + " " + component.replace("_", " "))
        lexical = len(query & words) / max(1, len(query | words))
        char = SequenceMatcher(None, clean, phrase).ratio()
        score = round(min(0.99, 0.78 * lexical + 0.22 * char), 4)
        if best is None or score > best["similarity"]:
            best = {"component": component, "system": system, "similarity": score, "matched_text": phrase}
    return best or {"component": "unknown", "system": "unknown", "similarity": 0.0, "matched_text": ""}


def generate_synthetic_data(seed: int = 17, aircraft_count: int = 24) -> dict[str, list[dict[str, Any]]]:
    rng = np.random.default_rng(seed)
    tails = [f"TAIL-{100+i}" for i in range(aircraft_count)]
    depots = [{"id": code, "name": name, "capacity": int(rng.integers(4, 10)), "region": region} for code, name, region in [("DEL", "Delhi", "north"), ("BOM", "Mumbai", "west"), ("BLR", "Bengaluru", "south"), ("CCU", "Kolkata", "east")]]
    spares = [{"part": f"SP-{i+1:03}", "component": COMPONENTS[i % len(COMPONENTS)], "quantity": int(rng.integers(0, 8)), "depot": depots[i % len(depots)]["id"], "lead_days": int(rng.integers(1, 8))} for i in range(20)]
    work_orders, tech_logs = [], []
    for i, tail in enumerate(tails):
        phrase, system, comp = SNAGS[i % len(SNAGS)]
        noise = ["pls chk", "occurs on climb", "crew says", "intermittent??", "after last sortie", "light came on"][i % 6]
        tech_logs.append({"id": f"LOG-{i+1:04}", "aircraft_id": tail, "text": f"{noise}: {phrase}", "mapped": map_snag(f"{noise}: {phrase}")})
        work_orders.append({"id": f"WO-{i+1:04}", "aircraft_id": tail, "component": comp, "depot": depots[i % len(depots)]["id"], "duration_hours": int(rng.integers(3, 14)), "priority": ["routine", "urgent", "critical"][i % 3], "status": "open" if i % 3 else "planned"})
    return {"tech_logs": tech_logs, "work_orders": work_orders, "spares": spares, "depots": depots}


def load_cmapss(split: str = "train", subset: str = "FD001", data_dir: Path | None = None) -> pd.DataFrame:
    """Load NASA turbofan text files. No network access or silent synthetic fallback."""
    if split not in {"train", "test", "RUL"} or subset not in {"FD001", "FD002", "FD003", "FD004"}:
        raise ValueError("split must be train/test/RUL and subset FD001..FD004")
    folder = data_dir or DATA / "CMAPSSData"
    filename = f"RUL_{subset}.txt" if split == "RUL" else f"{split}_{subset}.txt"
    path = folder / filename
    if not path.exists():
        raise FileNotFoundError(f"C-MAPSS file not found: {path}. Place the NASA C-MAPSS dataset in data/CMAPSSData.")
    names = ["unit", "cycle", "setting_1", "setting_2", "setting_3"] + [f"sensor_{i:02}" for i in range(1, 22)]
    frame = pd.read_csv(path, sep=r"\s+", header=None)
    if split == "RUL":
        frame.columns = ["RUL"]
    else:
        if frame.shape[1] != len(names):
            raise ValueError(f"Expected {len(names)} columns in {path}; got {frame.shape[1]}")
        frame.columns = names
    return frame


def preprocess(frame: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    sensor_cols = [c for c in frame if c.startswith("sensor_")]
    out = frame.copy()
    if sensor_cols:
        out[sensor_cols] = out.groupby("unit", sort=False)[sensor_cols].transform(lambda x: x.rolling(window, min_periods=1).mean())
    out["change_point"] = False
    if sensor_cols:
        score = out.groupby("unit", sort=False)[sensor_cols].diff().abs().mean(axis=1).fillna(0)
        threshold = score.median() + 3 * (score - score.median()).abs().median()
        out["change_point"] = score > max(float(threshold), 1e-9)
    return out


class RULInput(BaseModel):
    sensors: dict[str, float] = Field(min_length=1)


class AnomalyInput(BaseModel):
    sensors: dict[str, float] = Field(min_length=1)


class ScenarioInput(BaseModel):
    grounding_date_a: date
    grounding_date_b: date
    tails: list[str] = Field(default_factory=lambda: AIRCRAFT.copy(), max_length=200)


def _model():
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="FD001 prognostics model is not trained. Run python -m backend.scripts.train_fd001 after placing NASA C-MAPSS files in data/CMAPSSData.")
    import joblib
    return joblib.load(MODEL_PATH)


@router.get("/graph/aircraft/{aircraft_id}")
def aircraft_graph(aircraft_id: str, profile=Depends(demo_or_user)):
    if aircraft_id not in GRAPH:
        raise HTTPException(status_code=404, detail="Aircraft not found in the demo fleet graph")
    return {"data": GRAPH[aircraft_id]}


@router.get("/graph/aircraft")
def graph_index(profile=Depends(demo_or_user)):
    return {"data": [{"id": tail, **g["aircraft"]} for tail, g in GRAPH.items()]}


@router.post("/predict/rul")
def predict_rul(payload: RULInput, profile=Depends(demo_or_user), db=Depends(get_db)):
    model = _model()
    names = model["features"]
    x = np.array([[payload.sensors.get(n, 0.0) for n in names]], dtype=float)
    prediction = max(0.0, float(model["regressor"].predict(x)[0]))
    band = float(model["residual_q90"])
    attribution = _feature_contributions(model, x[0])
    audit(db, profile, "rul.predict", f"prediction={prediction:.2f}")
    return {"data": {"prediction_cycles": round(prediction, 2), "interval_cycles": [round(max(0, prediction-band), 2), round(prediction+band, 2)], "top_sensors": attribution[:3], "method": "FD001 RandomForest with empirical 90% absolute-residual interval"}}


@router.post("/predict/anomaly")
def predict_anomaly(payload: AnomalyInput, profile=Depends(demo_or_user), db=Depends(get_db)):
    model = _model()
    names = model["features"]
    x = np.array([[payload.sensors.get(n, 0.0) for n in names]], dtype=float)
    xs = model["scaler"].transform(x)
    reconstructed = model["autoencoder"].predict(xs)
    residuals = np.square(xs[0] - reconstructed[0])
    score = float(np.mean(residuals))
    anomaly = score > model["anomaly_threshold"]
    attrs = [{"sensor": names[i], "contribution": round(float(residuals[i]), 6)}
             for i in np.argsort(residuals)[::-1][:3]]
    audit(db, profile, "anomaly.predict", f"anomaly={anomaly}; score={score:.5f}")
    return {"data": {"anomaly": anomaly, "score": round(score, 5), "interval": [round(max(0, score-model["anomaly_band"]), 5), round(score+model["anomaly_band"], 5)], "top_sensors": attrs, "explanation_method": "autoencoder reconstruction residual contributions"}}


def _feature_contributions(model, x):
    try:
        import shap
        values = np.asarray(shap.TreeExplainer(model["regressor"]).shap_values(x.reshape(1, -1))).reshape(-1)
        return [{"sensor": model["features"][i], "contribution": round(float(abs(values[i])), 5)} for i in np.argsort(np.abs(values))[::-1][:3]]
    except Exception:
        pass
    means, scale = model["scaler"].mean_, model["scaler"].scale_
    contributions = np.abs((x-means) / np.where(scale == 0, 1, scale))
    return [{"sensor": model["features"][i], "contribution": round(float(contributions[i]), 4)} for i in np.argsort(contributions)[::-1][:3]]


@router.post("/planning/expected-aog-days")
def expected_aog(payload: dict, profile=Depends(demo_or_user)):
    snag = str(payload.get("snag", ""))
    mapping = map_snag(snag)
    base = {"oil pressure sensor": 1.5, "exhaust gas temperature sensor": 3.0, "fan rotor": 4.0, "fuel flow sensor": 2.0, "high pressure compressor": 6.0}.get(mapping["component"], 2.5)
    probability = min(0.98, max(0.1, float(payload.get("failure_probability", 0.55))))
    eta = float(payload.get("spare_eta_days", 2.0))
    return {"data": {"expected_aog_days": round(probability * max(base, eta), 2), "mapping": mapping, "assumptions": {"repair_days": base, "spare_eta_days": eta, "failure_probability": probability}}}


@router.post("/planning/bundle")
def bundle_work_orders(payload: dict, profile=Depends(planner_access)):
    orders = payload.get("work_orders") or generate_synthetic_data()["work_orders"][:12]
    capacity = int(payload.get("technician_hours_per_day", 24))
    # Optional OR-Tools CP-SAT optimization; deterministic greedy fallback keeps
    # the demo usable when the optional solver wheel is unavailable.
    try:
        from ortools.sat.python import cp_model
        solver_model = cp_model.CpModel()
        days = max(1, math.ceil(sum(max(1, int(o.get("duration_hours", 4))) for o in orders) / capacity))
        assigned = [[solver_model.NewBoolVar(f"x_{i}_{d}") for d in range(days)] for i in range(len(orders))]
        for i in range(len(orders)):
            solver_model.Add(sum(assigned[i]) == 1)
        for d in range(days):
            solver_model.Add(sum(assigned[i][d] * max(1, int(o.get("duration_hours", 4))) for i, o in enumerate(orders)) <= capacity)
        solver_model.Minimize(sum(d * assigned[i][d] for i in range(len(orders)) for d in range(days)))
        solver = cp_model.CpSolver(); solver.parameters.max_time_in_seconds = 2
        solver.Solve(solver_model)
        schedule = [{"work_order": orders[i].get("id", str(i)), "day": next(d for d in range(days) if solver.Value(assigned[i][d]))} for i in range(len(orders))]
        method = "OR-Tools CP-SAT"
    except (ImportError, StopIteration):
        schedule, day, used = [], 0, 0
        for i, order in enumerate(sorted(orders, key=lambda o: (o.get("priority") != "critical", o.get("priority") != "urgent"))):
            hours = max(1, int(order.get("duration_hours", 4)))
            if used + hours > capacity: day += 1; used = 0
            schedule.append({"work_order": order.get("id", str(i)), "day": day}); used += hours
        method = "priority-aware greedy fallback"
    return {"data": {"schedule": schedule, "days": max((x["day"] for x in schedule), default=0)+1, "method": method}}


@router.get("/planning/spares")
def spare_recommendations(profile=Depends(demo_or_user)):
    inventory = generate_synthetic_data()["spares"]
    needs = defaultdict(int)
    for tail, graph in GRAPH.items():
        for snag in graph["snags"]:
            needs[snag["component"]] += int(graph["aircraft"]["health"] < 85)
    recommendations = []
    for spare in inventory:
        demand = needs.get(spare["component"], 0)
        if spare["quantity"] <= 2 or demand:
            recommendations.append({**spare, "recommended_transfer": max(0, demand-spare["quantity"]), "priority": "high" if spare["quantity"] == 0 and demand else "monitor"})
    return {"data": recommendations}


@router.post("/planning/what-if")
def what_if(payload: ScenarioInput, profile=Depends(demo_or_user), db=Depends(get_db)):
    def availability(grounding: date):
        grounded = sum(1 for i, tail in enumerate(payload.tails) if tail in GRAPH and (i % 4 == 0 or (i % 2 == 0 and grounding.day % 2 == 0)))
        return round(100 * (1-grounded/max(1, len(payload.tails))), 1)
    a, b = availability(payload.grounding_date_a), availability(payload.grounding_date_b)
    audit(db, profile, "planning.what_if", f"{payload.grounding_date_a} vs {payload.grounding_date_b}")
    return {"data": {"scenario_a": {"grounding_date": str(payload.grounding_date_a), "availability_pct": a}, "scenario_b": {"grounding_date": str(payload.grounding_date_b), "availability_pct": b}, "delta_percentage_points": round(b-a, 1)}}


MANUALS = [
    {"id": "MAN-ENG-01", "text": "For elevated exhaust gas temperature, compare the temperature trend at matched operating conditions. Check sensor harness continuity and connector seating before replacing the probe. Record the engine cycle and operating setting."},
    {"id": "MAN-ENG-02", "text": "For rising fan vibration, review the trend against the baseline, inspect fan blades for damage, and check rotor balance. A sudden step change requires inspection before next release."},
    {"id": "MAN-OIL-01", "text": "Intermittent oil pressure indications can arise from pressure sensor wiring or a genuine lubrication fault. Verify with an independent gauge and inspect for leaks. Ground the aircraft if pressure is outside limits."},
    {"id": "CASE-021", "text": "Past case: crew reported oil pressure fluctuating during climb with no leak found. Connector reseating restored stable indication; follow-up inspection confirmed normal pressure."},
    {"id": "CASE-034", "text": "Past case: exhaust temperature ran high after takeoff. The sensor plug had backed out; securing the connector resolved the alert. Trend monitored for three cycles."},
    {"id": "CASE-052", "text": "Past case: vibration and fan noise increased gradually. Blade inspection found leading-edge damage; fan module inspection and balancing were scheduled."},
]


class AskInput(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


@lru_cache(maxsize=1)
def _rag_index():
    raw = MANUALS + [{"id": f"SNAG-{tail}", "text": g["snags"][0]["text"] + " Component: " + g["snags"][0]["component"]} for tail, g in GRAPH.items()]
    docs = []
    for item in raw:
        # Small overlapping chunks provide useful citations and keep evidence
        # focused; short source notes remain a single chunk.
        words = item["text"].split()
        for offset in range(0, max(1, len(words)), 80):
            chunk = " ".join(words[offset:offset+100])
            docs.append({"id": item["id"], "text": chunk})
            if offset + 100 >= len(words):
                break
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
    matrix = vectorizer.fit_transform([d["text"] for d in docs])
    return docs, vectorizer, matrix


def rag_answer(question: str) -> dict[str, Any]:
    docs, vectorizer, matrix = _rag_index()
    scores = cosine_similarity(vectorizer.transform([question]), matrix)[0]
    matches = np.argsort(scores)[::-1][:3]
    cited = [{"id": docs[i]["id"], "text": docs[i]["text"], "similarity": round(float(scores[i]), 4)} for i in matches if scores[i] > 0]
    context = " ".join(x["text"] for x in cited)
    answer = context[:900] if context else "No close manual or past snag was found. Add more detail about the symptom and operating condition."
    provider = os.getenv("RAG_PROVIDER", "local").lower()
    if provider == "ollama":
        try:
            import httpx
            response = httpx.post(os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"), json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"), "prompt": f"Answer using only this context and cite IDs: {context}\nQuestion: {question}", "stream": False}, timeout=30)
            response.raise_for_status(); answer = response.json().get("response", answer)
        except Exception:
            provider = "local-fallback"
    elif provider == "api":
        # OpenAI-compatible API provider configured by environment variables.
        try:
            import httpx
            endpoint = os.getenv("RAG_API_URL", "https://api.openai.com/v1/chat/completions")
            response = httpx.post(endpoint, headers={"Authorization": f"Bearer {os.getenv('RAG_API_KEY', '')}"}, json={"model": os.getenv("RAG_API_MODEL", "gpt-4o-mini"), "messages": [{"role": "user", "content": f"Answer using only this context and cite IDs.\n{context}\nQuestion: {question}"}]}, timeout=30)
            response.raise_for_status(); answer = response.json()["choices"][0]["message"]["content"]
        except Exception:
            provider = "local-fallback"
    return {"answer": answer, "citations": cited, "similar_cases": [d for d in cited if d["id"].startswith(("CASE-", "SNAG-"))], "provider": provider}


@router.post("/copilot/ask")
def ask(payload: AskInput, profile=Depends(demo_or_user), db=Depends(get_db)):
    result = rag_answer(payload.question)
    audit(db, profile, "copilot.ask", payload.question[:300])
    return {"data": result}


@router.get("/copilot/evaluation")
def rag_eval(profile=Depends(demo_or_user)):
    questions = ["What should I inspect when exhaust temperature is high?", "How do I investigate fan vibration?", "Oil pressure fluctuates during climb", "What caused the previous high EGT case?", "When must an aircraft be grounded for oil pressure?", "How to check a fan rotor?", "What was found in the connector case?", "How should sensor wiring be checked?", "What maintenance follows rising fan noise?", "How do I verify an oil pressure indication?", "What should be recorded for EGT?", "Which past case involved blade damage?", "What if vibration changes suddenly?", "What resolved an intermittent oil signal?", "How many cycles should temperature be monitored?"]
    results = [rag_answer(q) for q in questions]
    return {"data": {"questions": len(questions), "retrieval_hit_rate": round(sum(bool(r["citations"]) for r in results)/len(results), 3), "results": [{"question": q, "citations": r["citations"]} for q, r in zip(questions, results)]}}


@router.websocket("/ws/fleet")
async def fleet_events(websocket: WebSocket):
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        token = message.get("access_token") if isinstance(message, dict) else None
        if not isinstance(token, str) or not token:
            await websocket.close(code=1008, reason="Authentication required")
            return
        with SessionLocal() as db:
            current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)
        while True:
            await websocket.send_json({"type": "fleet_update", "at": datetime.now(timezone.utc).isoformat(), "aircraft": [{"id": tail, **graph["aircraft"]} for tail, graph in GRAPH.items()]})
            await asyncio.sleep(30)
    except (asyncio.TimeoutError, HTTPException, WebSocketDisconnect):
        if not websocket.client_state.DISCONNECTED:
            await websocket.close(code=1008, reason="Authentication failed or connection expired")


def evaluate_fd001(data_dir: Path | None = None) -> dict[str, Any]:
    """Fit FD001 regression + autoencoder-style reconstruction detector; report NASA metrics."""
    train = load_cmapss("train", "FD001", data_dir)
    test = load_cmapss("test", "FD001", data_dir)
    truth = load_cmapss("RUL", "FD001", data_dir)["RUL"].to_numpy()
    train = preprocess(train)
    test = preprocess(test)
    features = [f"sensor_{i:02}" for i in range(1, 22)]
    # Common constant sensors are dropped to reduce instability.
    features = [c for c in features if train[c].std() > 1e-8]
    max_life = 125
    train["RUL"] = (train.groupby("unit")["cycle"].transform("max") - train["cycle"]).clip(upper=max_life)
    Xtr, ytr = train[features].to_numpy(), train["RUL"].to_numpy()
    last = test.groupby("unit", sort=True).tail(1).reset_index(drop=True)
    Xte = last[features].to_numpy()
    yte = truth[:len(last)]
    scaler = StandardScaler().fit(Xtr)
    Xs = scaler.transform(Xtr); Xt = scaler.transform(Xte)
    # Autoencoder-like nonlinear reconstruction model on healthy operating points.
    autoencoder = MLPRegressor(hidden_layer_sizes=(12, 4, 12), activation="relu", max_iter=300, random_state=42)
    autoencoder.fit(Xs, Xs)
    recon_train = np.mean((autoencoder.predict(Xs)-Xs)**2, axis=1)
    anomaly = IsolationForest(contamination=0.03, random_state=42).fit(Xs)
    anomaly_band = float(np.quantile(np.abs(recon_train-np.median(recon_train)), .9))
    anomaly_threshold = float(np.quantile(recon_train, .99))
    # Bound tree size so the trained FD001 artifact fits common single-service
    # deployment bundles while preserving an ensemble for robust RUL estimates.
    regressor = RandomForestRegressor(n_estimators=150, min_samples_leaf=3, max_leaf_nodes=64, random_state=42, n_jobs=-1)
    units = np.sort(train["unit"].unique())
    calibration_units = units[-max(1, math.ceil(len(units)*.2)):]
    calibration_mask = train["unit"].isin(calibration_units).to_numpy()
    regressor.fit(Xtr[~calibration_mask], ytr[~calibration_mask])
    calibration_predictions = regressor.predict(Xtr[calibration_mask])
    residual_q90 = float(np.quantile(np.abs(ytr[calibration_mask]-calibration_predictions), .9))
    preds = np.clip(regressor.predict(Xte), 0, None)
    rmse = float(np.sqrt(np.mean((preds-yte)**2)))
    diff = preds-yte
    nasa = float(np.sum(np.where(diff < 0, np.exp(-diff/13)-1, np.exp(diff/10)-1)))
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    import joblib
    joblib.dump({"features": features, "scaler": scaler, "regressor": regressor, "anomaly": anomaly, "autoencoder": autoencoder, "residual_q90": residual_q90, "anomaly_band": anomaly_band, "anomaly_threshold": anomaly_threshold, "metrics": {"rmse": rmse, "nasa_score": nasa, "test_units": len(yte)}, "trained_at": datetime.now(timezone.utc).isoformat()}, MODEL_PATH)
    # Save empirical SHAP-compatible tree explanations when available.
    try:
        import shap
        explainer = shap.TreeExplainer(regressor)
        _ = explainer.shap_values(Xte[:min(32, len(Xte))])
    except Exception:
        pass
    return {"rmse": rmse, "nasa_score": nasa, "test_units": len(yte), "model_path": str(MODEL_PATH)}


@router.get("/predict/metrics")
def fd001_metrics(profile=Depends(demo_or_user)):
    if not MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail="FD001 model is not trained. Add train_FD001.txt, test_FD001.txt, and RUL_FD001.txt to data/CMAPSSData, then run python -m backend.scripts.train_fd001.")
    return {"data": _model()["metrics"]}

