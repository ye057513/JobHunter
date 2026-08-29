import sys, io, json, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
P = r"C:\Users\admin\AppData\Roaming\Tencent\Marvis\User\oAN1i2ceoM0vu22J0_ZM3Lb2YmCU\skills\custom\JobHunter"
sys.path.insert(0, P + r"\vendor")
sys.path.insert(0, P + r"\src")
import collectors as C
import config as _cfg
cc = ((_cfg.load_config().get("platforms") or {}).get("zhaopin") or {}).get("city_codes") or {}
print("zhaopin city_codes:", cc)
col = C.get_collector("zhaopin", cfg={"headless": False, "engine": "authed", "city_codes": cc})
for c in ("福州", "厦门", "泉州"):
    print("URL:", col.search_url_template("Java开发工程师", c))
try:
    jobs = col.collect(["Java开发工程师"], ["福州", "厦门", "泉州"], 1)
except Exception as e:
    print("采集异常:", repr(e)[:200]); raise SystemExit
print("采集返回:", len(jobs))
regs = collections.Counter((j.get("region") or "").strip() or "(空)" for j in jobs)
print("region 分布:", dict(list(regs.items())[:15]))
tgts = ("厦门", "福州", "泉州")
hit = sum(1 for j in jobs if (j.get("region") or "").strip() and any(t in (j.get("region") or "") for t in tgts))
print("命中目标城市:", hit, "/", len(jobs))
for j in jobs[:6]:
    print("   -", j.get("title"), "|", j.get("region"), "| url:", (j.get("url") or "")[:45])