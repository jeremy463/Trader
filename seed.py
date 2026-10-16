import time, sqlite3
c = sqlite3.connect("/tmp/ui/web.db"); now = time.time(); iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
c.execute("DELETE FROM events"); c.execute("DELETE FROM news_items"); c.execute("DELETE FROM feed_status")
for off, cur, imp, name, fc, pv, ac in ((3 * 3600, "USD", "high", "Core CPI m/m", "0.3%", "0.4%", ""), (20 * 3600, "EUR", "high", "ECB Press Conference", "", "", ""), (26 * 3600, "GBP", "medium", "GDP m/m", "0.1%", "0.2%", ""), (-2 * 3600, "USD", "medium", "Unemployment Rate", "4.2%", "4.1%", "4.4%")):
    c.execute("INSERT INTO events(time_utc,currency,impact,name,forecast,previous,actual,source) VALUES(?,?,?,?,?,?,?,?)", (iso(now + off), cur, imp, name, fc, pv, ac, "Seed"))
for i, (t, cu, se) in enumerate((("Fed signals rate cut path as inflation slows", "USD", -1.0), ("ECB hawkish: Lagarde says rate hike possible", "EUR", 1.0), ("BoE holds rates, Bailey cautious", "GBP", 0.0))):
    c.execute("INSERT INTO news_items(source,guid,title,link,published,currencies,sentiment,fetched) VALUES('Seed',?,?,?,?,?,?,?)", (f"g{i}", t, "https://example.org/" + str(i), int(now - 600 * (i + 1)), cu, se, int(now)))
c.execute("INSERT INTO feed_status(source,last_ok,last_try,last_error,items) VALUES('Seed',?,?,'',4)", (int(now), int(now))); c.execute("DELETE FROM journal"); c.commit(); print("reseeded at", iso(now))
