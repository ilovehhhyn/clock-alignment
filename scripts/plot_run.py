"""Plot a saved run without calling a model, allocating a GPU, or writing its database."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

FIELDS = ("call_index", "call_id", "scope", "status", "input_tokens", "output_tokens",
          "elapsed_seconds", "conservative_usd", "cumulative_conservative_usd")


def read_calls(directory):
    """Read the authoritative ledger in read-only mode, including unknown-cost holds."""
    database = (Path(directory) / "experiment.sqlite").resolve()
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        provider = connection.execute("SELECT value FROM meta WHERE key='provider'").fetchone()
        if provider is None:
            raise ValueError("Run has no saved provider identity")
        rows, cumulative = [], 0
        for i, row in enumerate(connection.execute("SELECT * FROM calls ORDER BY created,id"), 1):
            cumulative += row["charge_nano"]
            rows.append(dict(zip(FIELDS, (i, row["id"], row["scope"], row["status"],
                row["input_tokens"], row["output_tokens"], row["elapsed"],
                row["charge_nano"] / 1_000_000_000, cumulative / 1_000_000_000))))
        return {"provider": json.loads(provider[0]), "calls": rows}
    finally:
        connection.close()


def export_csv(data, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with destination.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(data["calls"])
    return destination


def render(data, destination):
    # Optional dependency: CSV/read-only tests require only the standard library.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = data["calls"]
    x = [r["call_index"] for r in rows]
    title = ("SCRIPTED FIXTURE — NO MODEL DATA" if data["provider"] == "scripted"
             else "Saved OpenAI run — observed API ledger")
    figure, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, layout="constrained")
    figure.suptitle(title)
    plots = (("output_tokens", "Output tokens\n(including reasoning)"),
             ("elapsed_seconds", "Request latency (s)"),
             ("cumulative_conservative_usd", "Conservative charge (USD)"))
    for axis, (key, label) in zip(axes, plots):
        y = [r[key] if r[key] is not None else float("nan") for r in rows]
        axis.plot(x, y, linewidth=1, marker=".", markersize=3)
        axis.set_ylabel(label)
        axis.grid(alpha=.2)
        if not rows:
            axis.text(.5, .5, "No calls recorded", transform=axis.transAxes, ha="center")
    unknown = [r for r in rows if r["status"] != "completed"]
    if unknown:
        axes[-1].scatter([r["call_index"] for r in unknown],
            [r["cumulative_conservative_usd"] for r in unknown],
            color="darkorange", label="Includes unresolved reservation", zorder=3)
        axes[-1].legend(loc="best")
    axes[-1].set_xlabel("Generation attempt index (game and analysis; ordered by ledger time)")
    figure.supxlabel("Current ledger charges include unknown holds; fixture USD is hypothetical.", fontsize=9)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    figure.savefig(destination, dpi=160, bbox_inches="tight", pad_inches=.15)
    plt.close(figure)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Existing saved run directory")
    args = parser.parse_args(argv)
    data = read_calls(args.out)
    directory = Path(args.out) / "diagnostics"
    csv_path = export_csv(data, directory / "runtime.csv")
    try:
        plot_path = render(data, directory / "runtime.png")
    except ModuleNotFoundError as error:
        if error.name not in {"matplotlib", "matplotlib.pyplot"}:
            raise
        parser.exit(2, "Plot dependency missing. Install with: python3 -m pip install '.[plots]'\n"
                       f"CSV saved: {csv_path}\n")
    print(json.dumps({"provider": data["provider"], "calls": len(data["calls"]),
                      "csv": str(csv_path), "plot": str(plot_path), "paid_api_calls": 0}))


if __name__ == "__main__":
    main()
