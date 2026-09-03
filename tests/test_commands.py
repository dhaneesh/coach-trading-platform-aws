from telegram.commands import parse
def test_buy(): assert parse("BUY GAIL 10")[1]["quantity"]==10
def test_entry(): assert parse("BUY SPARC 10 2nd")[1]["entry"]=="2nd"
