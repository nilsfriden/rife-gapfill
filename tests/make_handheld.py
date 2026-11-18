import sys, numpy as np
sys.argv = [sys.argv[0]]
import make_tests as mt
OPS = {}
for t in (31, 77, 140, 141, 199, 262, 318, 355):
    OPS[t] = "drop"
for t in (104, 230):
    OPS[t] = "repeat"
for t in (170, 290):
    OPS[t] = "extra"

def clip(name, corr, shake, seed):
    r = np.random.default_rng(seed)
    frames = []
    x, y = 200.0, 300.0
    jx = jy = 0.0
    for t in range(400):
        # AR(1) shake: corr 0.9 = smooth handheld sway, corr 0 = vibration
        jx = corr * jx + np.sqrt(1 - corr ** 2) * r.normal(0, shake)
        jy = corr * jy + np.sqrt(1 - corr ** 2) * r.normal(0, shake)
        x += 4.0 + jx; y += 0.5 + jy
        img = mt.crop(mt.TEX1, x, y) + r.normal(0, 2.0, (mt.H, mt.W, 3))
        frames.append(np.clip(img, 0, 255).astype(np.uint8))
    src, times = mt.apply_errors(frames, OPS)
    mt.write(name, src, times, "clips")

clip("handheld", 0.9, 1.5, 11)
clip("vibration", 0.0, 1.2, 12)
