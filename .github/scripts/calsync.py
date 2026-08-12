#!/usr/bin/env python3
"""Reads the private iCal feed of a Google Calendar and publishes a clean JSON
copy into the tracker's secret gist. Runs in GitHub Actions every 15 minutes.

Secrets used:  GCAL_ICS_URL, SYNC_GIST_ID, GIST_TOKEN
Nothing is ever written into the public repository.
"""
import os, re, json, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone, date

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("America/Chicago")
except Exception:
    LOCAL = timezone(timedelta(hours=-5))

ICS = os.environ["GCAL_ICS_URL"]
GID = os.environ["SYNC_GIST_ID"]
TOK = os.environ["GIST_TOKEN"]
CAL_FILE = "calendar-feed.json"
BACK_DAYS, FWD_DAYS = 60, 400


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "case-tracker-calendar-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def unfold(text):
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")).strip()


def parse_dt(raw, params):
    """returns (date_str, time_str, all_day)"""
    raw = raw.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", raw):
        d = datetime.strptime(raw[:8], "%Y%m%d")
        return d.strftime("%Y-%m-%d"), "", True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", raw)
    if not m:
        return None, "", False
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":                                  # UTC → local
        dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), False


def expand(ev, rrule, lo, hi):
    """very small RRULE expander — DAILY/WEEKLY/MONTHLY/YEARLY with COUNT/UNTIL/INTERVAL"""
    parts = dict(p.split("=", 1) for p in rrule.split(";") if "=" in p)
    freq = parts.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [ev]
    step = int(parts.get("INTERVAL", 1) or 1)
    count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
    until = None
    if parts.get("UNTIL"):
        u = re.sub(r"[TZ].*$", "", parts["UNTIL"])
        try:
            until = datetime.strptime(u, "%Y%m%d").date()
        except ValueError:
            until = None
    delta = {"DAILY": timedelta(days=step), "WEEKLY": timedelta(weeks=step)}.get(freq)
    out, cur = [], datetime.strptime(ev["date"], "%Y-%m-%d").date()
    for i in range(count or 200):
        if until and cur > until:
            break
        if cur > hi:
            break
        if cur >= lo:
            c = dict(ev)
            c["date"] = cur.strftime("%Y-%m-%d")
            c["uid"] = ev["uid"] if i == 0 else "%s_%s" % (ev["uid"], cur.strftime("%Y%m%d"))
            out.append(c)
        if delta:
            cur = cur + delta
        else:                                              # MONTHLY / YEARLY
            months = step if freq == "MONTHLY" else step * 12
            y, m = cur.year + (cur.month - 1 + months) // 12, (cur.month - 1 + months) % 12 + 1
            day = min(cur.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
            cur = date(y, m, day)
        if len(out) > 120:
            break
    return out or [ev]


def main():
    raw = fetch(ICS)
    lines = unfold(raw)
    today = datetime.now(LOCAL).date()
    lo, hi = today - timedelta(days=BACK_DAYS), today + timedelta(days=FWD_DAYS)

    events, cur, inside = [], None, False
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur, inside = {"status": "confirmed"}, True
            continue
        if line == "END:VEVENT":
            inside = False
            if cur and cur.get("uid") and cur.get("date"):
                rr = cur.pop("_rrule", "")
                base = {k: cur.get(k, "") for k in
                        ("uid", "title", "date", "time", "end", "loc", "desc", "status", "allDay")}
                for e in (expand(base, rr, lo, hi) if rr else [base]):
                    d = datetime.strptime(e["date"], "%Y-%m-%d").date()
                    if lo <= d <= hi:
                        events.append(e)
            cur = None
            continue
        if not inside or cur is None or ":" not in line:
            continue
        head, val = line.split(":", 1)
        bits = head.split(";")
        name = bits[0].upper()
        params = dict(b.split("=", 1) for b in bits[1:] if "=" in b)
        if name == "UID":
            cur["uid"] = val.strip()
        elif name == "SUMMARY":
            cur["title"] = unescape(val)
        elif name == "LOCATION":
            cur["loc"] = unescape(val)
        elif name == "DESCRIPTION":
            cur["desc"] = unescape(val)[:400]
        elif name == "STATUS":
            cur["status"] = val.strip().lower()
        elif name == "RRULE":
            cur["_rrule"] = val.strip().upper()
        elif name == "RECURRENCE-ID":
            d, t, _ = parse_dt(val, params)
            if d and cur.get("uid"):
                cur["uid"] = "%s_%s" % (cur["uid"], d.replace("-", ""))
        elif name == "DTSTART":
            d, t, allday = parse_dt(val, params)
            if d:
                cur["date"], cur["time"], cur["allDay"] = d, t, allday
        elif name == "DTEND":
            d, t, _ = parse_dt(val, params)
            cur["end"] = t or ""

    # newest wins on duplicate uid
    seen = {}
    for e in events:
        seen[e["uid"] + "|" + e["date"]] = e
    events = sorted(seen.values(), key=lambda e: (e["date"], e.get("time") or ""))

    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "window": {"from": lo.isoformat(), "to": hi.isoformat()},
        "count": len(events),
        "events": events,
    }
    body = json.dumps({"files": {CAL_FILE: {"content": json.dumps(payload)}}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/gists/" + GID, data=body, method="PATCH",
        headers={"Authorization": "Bearer " + TOK, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        print("published %d events to gist (HTTP %s)" % (len(events), r.status))


if __name__ == "__main__":
    main()
