import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

URL = "https://civitai.com/api/trpc/hatTurf.getFlags?input=%7B%22json%22%3A%7B%22event%22%3A%22birthday2026%22%7D%7D"
POLL_INTERVAL_SECONDS = 2
RATE_WINDOW_SECONDS = 300  # hats/min is measured over roughly this window
CLOSE_PCT = 0.05  # a flag is "CLOSE" if the lead is under 5% of the leader's hats...
CLOSE_HATS = 100  # ...or under this many hats
SAVE_SNAPSHOTS = False
LOG_FILE = "flag_race_snapshots.jsonl"

# Overall team points from the event page (edit these; set to {} to hide the projection).
# Today's flag pots are added on top, since they pay out when the flag day closes.
BASE_SCORES = {
    "Pink": 131_839_954,
    "Green": 128_056_816,
    "Blue": 120_109_829,
    "Yellow": 77_127_104,
}
# Hat cap / regen anchors by rank; 2nd and 4th are extrapolated in a straight line.
CAP_1ST, REGEN_1ST = 14, 0.74
CAP_2ND, REGEN_2ND = 15, 0.79
CAP_3RD, REGEN_3RD = 18, 0.92
CAP_4TH, REGEN_4TH = 40, 2.00


# Measured hat cap / regen multiplier / regen minutes per team. These override the
# rank-based estimate above. Update them whenever you read new values in-game.
TEAM_STATS = {
    "Pink": (14, 0.74, 5),
    "Green": (15, 0.79, 4),
    "Blue": (18, 0.92, 4),
    "Yellow": (40, 2.00, 2),
}
FOCUS_TEAM = "Blue"  # shows a "where to place hats" table for this team; None to hide

TEAMS = ["Yellow", "Blue", "Green", "Pink"]
COLORS = {
    "Pink": "\033[95m",
    "Green": "\033[92m",
    "Blue": "\033[94m",
    "Yellow": "\033[93m",
}
RESET, BOLD, DIM, RED = "\033[0m", "\033[1m", "\033[2m", "\033[91m"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://civitai.com",
}


def paint(team, text):
    return f"{COLORS.get(team, '')}{text}{RESET}"


def fetch_flags():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
        return raw["result"]["data"]["json"]


def format_time_remaining(closes_at_str):
    closes_at = datetime.fromisoformat(closes_at_str.replace("Z", "+00:00"))
    delta = closes_at - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "CLOSED"
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


def leader_of(hats):
    """Return (leader or None on a tie, margin over 2nd, leader hats)."""
    ranked = sorted(hats.items(), key=lambda x: x[1], reverse=True)
    (team, top), (_, second) = ranked[0], ranked[1]
    margin = top - second
    return (team if margin > 0 else None), margin, top


class State:
    def __init__(self):
        self.history = deque()  # (timestamp, {image_id: hats_by_team})
        self.leaders = {}  # image_id -> leader team (None for tie)
        self.events = deque(maxlen=8)
        self.errors = 0

    def previous(self):
        return self.history[-1][1] if self.history else {}

    def rate_baseline(self, now):
        """Oldest sample inside the rate window (or the oldest we have)."""
        for ts, snap in self.history:
            if now - ts <= RATE_WINDOW_SECONDS:
                return ts, snap
        return None, None


def team_totals(snapshot):
    totals = {t: 0 for t in TEAMS}
    for hats in snapshot.values():
        for t in TEAMS:
            totals[t] += hats.get(t, 0)
    return totals


def print_focus(flags, index, team):
    """Where should `team` place hats? Leads to defend first, then best pot per hat needed."""
    print()
    print(f"{BOLD}{paint(team, team.upper() + ' FOCUS')}{RESET} (hats needed to take the lead = rival hats - yours + 1)")
    print(f"{'#':>2} {'Pot':>9} {'Action':<7} {'Rival':<7} {'Hats':>7} {'Pot/hat':>9}")
    defend, chase = [], []
    for f in flags:
        hats = f["hatsByTeam"]
        mine = hats.get(team, 0)
        rival, rival_hats = max(
            ((t, v) for t, v in hats.items() if t != team), key=lambda x: x[1]
        )
        gap = rival_hats - mine
        n = index[f["imageId"]]
        if gap < 0:
            defend.append((-gap, n, f["pot"], rival))
        else:
            chase.append((f["pot"] / (gap + 1), gap + 1, n, f["pot"], rival))
    for cushion, n, pot, rival in sorted(defend):
        print(f"{n:>2} {pot:>9,} {'DEFEND':<7} {paint(rival, f'{rival:<7}')} {'+' + format(cushion, ','):>7} {'':>9}")
    for ratio, need, n, pot, rival in sorted(chase, reverse=True):
        print(f"{n:>2} {pot:>9,} {'CHASE':<7} {paint(rival, f'{rival:<7}')} {need:>7,} {ratio:>9,.0f}")
    print(f"{DIM}DEFEND: your cushion over the next team. CHASE: hats to pass the leader; higher pot/hat = better target.{RESET}")


def render(data, state):
    now = time.time()
    flags = data.get("flags", [])
    prev = state.previous()
    current = {f["imageId"]: f["hatsByTeam"] for f in flags}
    index = {f["imageId"]: i + 1 for i, f in enumerate(flags)}

    # Lead changes since the last poll
    stamp = datetime.now().strftime("%H:%M:%S")
    for f in flags:
        img = f["imageId"]
        lead, margin, _ = leader_of(f["hatsByTeam"])
        if img in state.leaders and state.leaders[img] != lead:
            old = state.leaders[img] or "TIE"
            new = lead or "TIE"
            state.events.appendleft(
                f"{stamp}  Flag #{index[img]}: {old} -> {new} (lead {margin:,})"
            )
        state.leaders[img] = lead

    # Hats per minute for each team over the rate window
    base_ts, base_snap = state.rate_baseline(now)
    rates = {t: 0.0 for t in TEAMS}
    if base_snap is not None and now - base_ts >= 5:
        minutes = (now - base_ts) / 60
        before, after = team_totals(base_snap), team_totals(current)
        rates = {t: (after[t] - before[t]) / minutes for t in TEAMS}

    # Clear screen and go to top-left
    sys.stdout.write("\033[H\033[J")
    line = "=" * 96
    print(line)
    print(
        f"{BOLD}CIVITAI 4TH BIRTHDAY - FLAG RACE LIVE{RESET} | Flag Day #{data['flagDayId']} | "
        f"Updated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(
        f"Closes (UTC): {data['closesAt']} | Time left: {BOLD}{format_time_remaining(data['closesAt'])}{RESET} "
        f"| Frozen: {data['frozen']} | Closed: {data['closed']}"
    )
    print(line)

    head = (
        f"{'#':>2} {'Image ID':<10} {'Pot':>9} {'Plyrs':>5} {'Leader':<7} {'Lead':>6} {'Status':<6} | "
        + " ".join(f"{t:>11}" for t in TEAMS)
    )
    print(head)
    print("-" * 96)

    projected = {t: 0 for t in TEAMS}
    flags_led = {t: 0 for t in TEAMS}
    for f in flags:
        img, pot, hats = f["imageId"], f["pot"], f["hatsByTeam"]
        lead, margin, top = leader_of(hats)
        if lead:
            projected[lead] += pot
            flags_led[lead] += 1
            label = paint(lead, f"{lead:<7}")
        else:
            label = f"{'TIE':<7}"
        close = margin <= CLOSE_HATS or (top and margin / top < CLOSE_PCT)
        status = f"{RED}CLOSE{RESET}" if close else f"{DIM}safe{RESET}  "
        cells = []
        for t in TEAMS:
            val = hats.get(t, 0)
            before = prev.get(img, {}).get(t)
            delta = val - before if before is not None else 0
            text = f"{val:,}" + (f"+{delta}" if delta > 0 else "")
            cell = f"{text:>11}"
            cells.append(BOLD + paint(t, cell) if t == lead else cell)
        print(
            f"{index[img]:>2} {img:<10} {pot:>9,} {f.get('players', 0):>5} {label} {margin:>6,} {status} | "
            + " ".join(cells)
        )
    print("-" * 96)

    # Team summary
    totals = team_totals(current)
    last_totals = team_totals(prev) if prev else totals
    print(f"{BOLD}TEAM SUMMARY (if the race ended now){RESET}")
    print(f"{'Team':<8} {'Flags':>5} {'Pot won':>11} {'Total hats':>11} {'Since poll':>10} {'Hats/min':>9}")
    for t in sorted(TEAMS, key=lambda x: projected[x], reverse=True):
        gain = totals[t] - last_totals[t]
        print(
            f"{paint(t, f'{t:<8}')} {flags_led[t]:>5} {projected[t]:>11,} {totals[t]:>11,} "
            f"{('+' + str(gain)) if gain else '0':>10} {rates[t]:>9.1f}"
        )

    # Projected overall standings, hat caps and regen by rank
    if BASE_SCORES:
        print()
        print(f"{BOLD}PROJECTED OVERALL STANDINGS{RESET} (event-page score + today's pot)")
        print(f"{'Rank':<5} {'Team':<8} {'Now':>13} {'+ Today':>11} {'Projected':>13} {'Hat caps':>9} {'Regen':>7}")
        rows = sorted(
            TEAMS, key=lambda t: BASE_SCORES.get(t, 0) + projected[t], reverse=True
        )
        cap_step = (CAP_3RD - CAP_1ST) / 2
        regen_step = (REGEN_3RD - REGEN_1ST) / 2
        for i, t in enumerate(rows):
            base = BASE_SCORES.get(t, 0)
            if t in TEAM_STATS:
                cap, regen = TEAM_STATS[t][0], TEAM_STATS[t][1]
            else:
                cap = round(CAP_1ST + cap_step * i)
                regen = REGEN_1ST + regen_step * i
            print(
                f"{i + 1:<5} {paint(t, f'{t:<8}')} {base:>13,} {projected[t]:>11,} {base + projected[t]:>13,} "
                f"{cap:>9} {'x' + format(regen, '.3f'):>7}"
            )
        print(f"{DIM}Hat caps/regen: from TEAM_STATS where set, otherwise estimated by rank.{RESET}")

    if FOCUS_TEAM in TEAMS:
        print_focus(flags, index, FOCUS_TEAM)

    # Recent lead changes
    print()
    print(f"{BOLD}RECENT LEAD CHANGES{RESET}")
    if state.events:
        for e in state.events:
            print("  " + e)
    else:
        print(f"  {DIM}none yet since the tracker started{RESET}")
    print(f"\n{DIM}Polling every {POLL_INTERVAL_SECONDS}s. Ctrl+C to quit.{RESET}")
    sys.stdout.flush()

    state.history.append((now, current))
    while state.history and now - state.history[0][0] > RATE_WINDOW_SECONDS * 2:
        state.history.popleft()


def save_snapshot(data):
    record = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main():
    os.system("")  # enables ANSI colors in the Windows terminal
    state = State()
    try:
        while True:
            try:
                data = fetch_flags()
                state.errors = 0
                if SAVE_SNAPSHOTS:
                    save_snapshot(data)
                render(data, state)
                if data.get("closed"):
                    print("\nFlag Day is closed! Final teamPoints:", data.get("teamPoints"))
                    break
            except urllib.error.HTTPError as e:
                state.errors += 1
                print(f"[{datetime.now().strftime('%H:%M:%S')}] HTTP Error {e.code}: {e.reason} (x{state.errors})")
            except Exception as e:
                state.errors += 1
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e} (x{state.errors})")
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()