import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rife_gapfill as g
def run(name, **kw):
    opt = g.Options(**kw)
    an = g.analyze(f"clips/{name}.mov", opt, log=lambda *a: None)
    truth = json.load(open(f"clips/{name}.truth.json"))["events"]
    T = {(e["frame"], e["type"]): e.get("count", 0) for e in truth}
    D = {(e.frame, e.kind): e for e in an["events"] if e.kind in ("missing", "repeat", "long")}
    tp = [k for k in T if k in D]
    fn = [k for k in T if k not in D]
    fp = [k for k in D if k not in T]
    wrong_count = [k for k in tp if k[1] == "missing" and D[k].count != T[k]]
    applied_ok = sum(1 for k in tp if D[k].apply)
    applied_fp = [k for k in fp if D[k].apply]
    stats = {"missed": len(fn), "false_applied": len(applied_fp), "wrong_count": len(wrong_count),
             "applied_ok": applied_ok, "truth": len(T)}
    print(f"{name:9s} truth {len(T):2d}  found {len(tp):2d}  missed {len(fn)}  false {len(fp)} (auto-applied {len(applied_fp)})"
          f"  wrong count {len(wrong_count)}  auto-applied correct {applied_ok}/{len(T)}")
    for k in fn: print("    MISSED", k, "truth count", T[k])
    for k in fp: e = D[k]; print(f"    FALSE  {k} conf {e.conf:.2f} {e.note}")
    for k in wrong_count: print("    COUNT", k, "found", D[k].count, "truth", T[k])
    for k in tp:
        if not D[k].apply: print(f"    LOWCONF {k} conf {D[k].conf:.2f} {D[k].note}")
    cuts = [e.frame for e in an["events"] if e.kind == "cut"]
    print("    cuts:", cuts, " period:", an["stats"]["period"])
    return stats
# --check: exit 1 unless every listed clip is detected perfectly
# (all events found and auto-applied, correct counts, nothing false applied)
args = [a for a in sys.argv[1:] if a != "--check"]
failed = []
for n in args or ["clean", "random", "periodic"]:
    s = run(n)
    if s["missed"] or s["false_applied"] or s["wrong_count"] or s["applied_ok"] != s["truth"]:
        failed.append(n)
if "--check" in sys.argv:
    print("FAILED: " + ", ".join(failed) if failed else "all checks passed")
    sys.exit(1 if failed else 0)
