from monitor.signals import extract_signals

def test_signal():
    rows=[["Date","NSE CODE","N. Entries","1st Entry","STATUS OF COLUMN D","2nd Entry","STATUS OF COLUMN F","3rd Entry","STATUS OF COLUMN H"],["2026-08-28","GAIL","3","173","Activated","166","NO","158","NO"]]
    s=extract_signals(rows,"T20-20_AUG_26")
    assert len(s)==1 and s[0]["nse"]=="GAIL" and s[0]["entryValue"]==173.0
