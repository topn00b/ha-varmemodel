"""Genkør en plan fra Varmemodels diagnostik (»Download diagnostik« på integrationen) uden for huset.

Brug: python tools/replay_plan.py <diagnostik.json> [--kilde 1] [--sæt flow_margin=2 flow_raise_max=0 priority=comfort …]

Udskriver planens nøgletal, timerne og rummenes laveste forudsagte temperatur. Med --sæt kan enkelte argumenter
ændres, så en rettelse kan afprøves mod en rigtig situation, før den installeres.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from custom_components.varmemodel.planner import plan_from_dump  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("fil")
    ap.add_argument("--kilde", type=int, default=1)
    ap.add_argument("--sæt", nargs="*", default=[], metavar="navn=tal")
    a = ap.parse_args()
    with open(a.fil, encoding="utf-8") as f:
        d = json.load(f)
    d = d.get("data", d)
    dump = d["sources"][f"kilde_{a.kilde}"]["plan_input"]
    def val(v: str) -> float | int | str:
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return v  # fx priority=comfort

    override = {k: val(v) for k, v in (x.split("=", 1) for x in a.sæt)}
    p = plan_from_dump(dump, **override)
    print(f"{p.heat_kwh:.1f} kWh varme · {p.el_kwh:.1f} kWh el · {p.cost_kr:.1f} kr · manko {p.deficit:.2f} °C · "
          f"fremløb hævet {p.flow_raise:.0f} °C")
    print(p.reason)
    for c in p.choices:  # prioritetens tre trin: hvad hvert af dem ville vælge
        room = f"{c['rum']} holder" if c["temp"] is None else f"{c['rum']} {c['temp']:.1f} °C, under grænsen i {c['timer']} t"
        print(f"  {c['trin']:8s} +{c['hævet']} °C · {c['kr']:.1f} kr · {c['kwh']:.1f} kWh varme · {room}")
    print("time  pris   ude  blok   kW  fremløb")
    for x, h in zip(p.hours, dump["hours"]):
        flow = x["flow"] if x["flow"] is not None else "–"
        mark = "rød " if x["blocked"] else ("del " if x.get("free") else "    ")  # del = delvist fri time (kun timens frie del)
        print(f"{h['h']:>4}  {x['price']:.2f} {x['t_out']:>5.1f}  {mark} {x['heat_kw']:>4.2f}  {flow}")
    for r in dump["rooms"]:
        s = p.rooms.get(r["name"]) or []
        if s:
            print(f"{r['name']}: nu {r['t_now']:.1f} · laveste {min(s):.2f} om {s.index(min(s))} t · gulv {r['t_min']:.1f}")


if __name__ == "__main__":
    main()
