f = {
    "type": "missing_information",
    "title": "DPO Contact Not Publicly Disclosed"
}
check = (f.get("check_id") or f.get("type") or f.get("title") or "").lower()
print(check)
