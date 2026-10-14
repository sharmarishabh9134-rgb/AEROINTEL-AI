from backend.app.fleet_intelligence import generate_synthetic_data, map_snag


def main():
    print("Five noisy snag-to-component examples")
    for item in generate_synthetic_data()["tech_logs"][:5]:
        result = map_snag(item["text"])
        print(f"{item['text']}\n  -> {result['component']} ({result['similarity']:.3f})")


if __name__ == "__main__":
    main()
