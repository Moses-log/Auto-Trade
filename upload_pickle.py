import base64
import os
import urllib.request
import json

secret = input("Enter your webhook secret: ")

path = os.path.expanduser("~/.tokens/robinhood.pickle")
with open(path, "rb") as f:
    pickle_b64 = base64.b64encode(f.read()).decode()

body = json.dumps({"secret": secret, "pickle_b64": pickle_b64}).encode()
req = urllib.request.Request(
    "https://auto-trade-ro8k.onrender.com/robinhood-upload-pickle",
    data=body,
    headers={"Content-Type": "application/json"},
)
try:
    resp = urllib.request.urlopen(req)
    print(resp.read().decode())
except urllib.error.HTTPError as e:
    print("Status:", e.code)
    print("Error:", e.read().decode())
