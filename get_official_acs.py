import requests
import re
import json

r = requests.get('https://ceomanipur.nic.in/eroll.aspx', timeout=15)
matches = re.findall(r'<option value="(\d+)">([^<]+)</option>', r.text)
ac_dict = {int(m[0]): m[1].strip() for m in matches if m[0] != "0"}
print(f"Loaded {len(ac_dict)} official constituencies.")
with open("official_acs.json", "w", encoding="utf-8") as f:
    json.dump(ac_dict, f, indent=2)
print("Saved to official_acs.json")
