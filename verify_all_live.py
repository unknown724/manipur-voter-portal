import urllib.request, json

test_ids = [52062, 351790, 351801, 1186192]
for vid in test_ids:
    url = f"http://127.0.0.1:8000/api/voter/{vid}/household_map"
    req = urllib.request.urlopen(url)
    data = json.loads(req.read())
    ps = data["polling_station"]
    print(f"=== Elector: {data['elector_info']['name']} (ID {vid}) ===")
    print(f"  Official Booth: {ps['name']}")
    print(f"  Building: {ps['building_name']} (Wing: {ps['wing'] or 'N/A'})")
    print(f"  Locality: {ps['locality']}")
    print(f"  Coordinates: {ps['latitude']}, {ps['longitude']}")
    print(f"  Google Maps: {ps['google_maps_url']}")
    print(f"  Directions: {ps['google_maps_directions_url']}")
    print(f"  Household Roll Address: House #{data['elector_info']['house_no']}")
    print(f"  Family Electors Voting Here: {data['family']['total_count']}")
    print()
