"""Train and evaluate the synthetic maintenance demo model explicitly."""
import json

from backend.app.ml_analytics import NOTICE, train_model


def main() -> None:
    result = train_model()
    print(json.dumps({**result, "notice": NOTICE}, indent=2))


if __name__ == "__main__":
    main()
