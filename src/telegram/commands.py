import re
BUY_RE=re.compile(r"^(?:/)?buy\s+([A-Za-z0-9:_-]+)\s+(\d+)(?:\s+(1st|2nd|3rd|1|2|3|first|second|third))?$",re.I)

def normalize_symbol(v):
    v=v.strip().upper()
    return v.split(":",1)[1] if ":" in v else v

def normalize_entry(v):
    if not v: return None
    return {"1":"1st","1st":"1st","first":"1st","2":"2nd","2nd":"2nd","second":"2nd","3":"3rd","3rd":"3rd","third":"3rd"}.get(v.strip().lower())

def parse(text):
    m=BUY_RE.match(text.strip())
    if m:
        return "buy", {"symbol":normalize_symbol(m.group(1)),"quantity":int(m.group(2)),"entry":normalize_entry(m.group(3))}
    t=text.strip().lower()
    if t in ("/confirm","confirm"): return "confirm", {}
    if t in ("/cancel","cancel"): return "cancel", {}
    return None
