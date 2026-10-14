import json
from datetime import date, timedelta
from backend.app.fleet_intelligence import generate_synthetic_data, GRAPH


def run():
    data = generate_synthetic_data()
    tails = list(GRAPH)
    reactive_grounded = sum(g["aircraft"]["health"] < 90 for g in GRAPH.values())
    bundled_grounded = max(0, reactive_grounded - 2)
    return {"baseline": {"strategy": "reactive", "grounded_aircraft": reactive_grounded, "availability_pct": round(100*(len(tails)-reactive_grounded)/len(tails),1), "estimated_aog_days": reactive_grounded*3.2}, "after": {"strategy": "bundled preventative", "grounded_aircraft": bundled_grounded, "availability_pct": round(100*(len(tails)-bundled_grounded)/len(tails),1), "estimated_aog_days": bundled_grounded*2.1}, "work_orders_bundled": len(data["work_orders"]), "spares_tracked": len(data["spares"]), "note": "Fictional deterministic demonstration metrics; not measured fleet performance."}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
