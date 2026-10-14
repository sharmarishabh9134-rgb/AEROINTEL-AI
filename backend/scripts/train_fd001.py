import json
from backend.app.fleet_intelligence import evaluate_fd001


if __name__ == "__main__":
    try:
        print(json.dumps(evaluate_fd001(), indent=2))
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
