import json, sys, os
sys.path.insert(0, "/home/claude/v2")
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
    print(f"{name:9s} truth {len(T):2d}  found {len(tp):2d}  missed {len(fn)}  false {len(fp)} (auto-applied {len(applied_fp)})"
          f"  wrong count {len(wrong_count)}  auto-applied correct {applied_ok}/{len(T)}")
    for k in fn: print("    MISSED", k, "truth count", T[k])
    for k in fp: e = D[k]; print(f"    FALSE  {k} conf {e.conf:.2f} {e.note}")
    for k in wrong_count: print("    COUNT", k, "found", D[k].count, "truth", T[k])
    for k in tp:
        if not D[k].apply: print(f"    LOWCONF {k} conf {D[k].conf:.2f} {D[k].note}")
    cuts = [e.frame for e in an["events"] if e.kind == "cut"]
    print("    cuts:", cuts, " period:", an["stats"]["period"])
    return an
for n in sys.argv[1:] or ["clean", "random", "periodic"]:
    run(n)
