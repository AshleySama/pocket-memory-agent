"""一键测试工具意图解析（需先启动服务）"""
import urllib.request
import json

tests = [
    "1分钟后提醒我喝水",
    "5分钟后提醒我开会",
    "开始番茄钟",
    "明天下午3点提醒我交报告",
    "每天早上9点提醒站会",
]

for text in tests:
    req = urllib.request.Request(
        "http://127.0.0.1:8765/api/tools",
        data=json.dumps({"input": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        data = json.loads(r.read().decode())
        if data.get("tool") and data["tool"] != "none":
            result = data.get("result", {})
            status = "OK" if result.get("ok") else "FAIL"
            print(f"[{status}] {text}")
            print(f"       tool={data['tool']} result={result}")
        else:
            print(f"[FAIL] {text}")
            print(f"       {data}")
    except Exception as e:
        print(f"[ERR]  {text} -> {e}")
    print()
