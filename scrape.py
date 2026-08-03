import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENDPOINT = "https://hackerone.com/graphql"

QUERY = """
query CountryBoard(
  $key: LeaderboardKeyEnum!, $year: Int, $quarter: Int, $first: Int,
  $after: String, $filter: String, $user_type: String, $engagement_type: String
) {
  leaderboard_entries(
    key: $key, year: $year, quarter: $quarter, first: $first, after: $after,
    filter: $filter, user_type: $user_type, engagement_type: $engagement_type
  ) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        ... on HighRepByEngagementTypeAndCountryLeaderboardEntry { user { ...U } }
        ... on HighestReputationByCountryLeaderboardEntry { user { ...U } }
      }
    }
  }
}
fragment U on User {
  id username country reputation rank signal impact
  resolved_report_count thanks_items_total_count
}
"""

ENGAGEMENTS = [
    ("bbp", "HIGHEST_REPUTATION_BY_ENGAGEMENT_TYPE_AND_COUNTRY"),
    ("vdp", "HIGHEST_REPUTATION_BY_ENGAGEMENT_TYPE_AND_COUNTRY"),
    (None, "HIGHEST_REPUTATION_BY_COUNTRY"),
]
USER_TYPES = ("individual", "business")

COLUMNS = [
    "country_rank", "username", "reputation", "worldwide_rank",
    "resolved_reports", "thanks_items", "signal", "impact",
    "user_type", "profile", "user_id",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


class GraphQLError(RuntimeError):
    """Server menjawab tapi menolak query. Retry tidak akan membantu."""


def post(query, variables, retries=6):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={
            "content-type": "application/json",
            "accept": "*/*",
            "user-agent": UA,
            "x-product-area": "leaderboard",
            "x-product-feature": "details",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
            if "errors" in payload:
                raise GraphQLError(str(payload["errors"])[:200])
            return payload["data"]
        except GraphQLError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries - 1:
                wait = int(exc.headers.get("Retry-After", 2 ** attempt))
                print(f"    ! 429 rate limited - waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except Exception as exc:
            if attempt == retries - 1:
                raise
            print(f"    ! {exc} - retry {attempt + 1}", file=sys.stderr)
            time.sleep(2 ** attempt)


def sweep(base, delay):
    """Paging satu slice leaderboard, yield dict user."""
    cursor = None
    n = 0
    while True:
        conn = post(QUERY, {**base, "first": 100, "after": cursor})["leaderboard_entries"]
        for edge in conn["edges"]:
            user = (edge.get("node") or {}).get("user")
            if user:
                n += 1
                yield user
        if not conn["pageInfo"]["hasNextPage"]:
            return
        cursor = conn["pageInfo"]["endCursor"]
        time.sleep(delay)


def load_checkpoint(path):
    if not os.path.exists(path):
        return {}, set()
    with open(path) as fh:
        data = json.load(fh)
    return data.get("hackers", {}), set(tuple(p) for p in data.get("done_periods", []))


def save_checkpoint(path, hackers, done_periods):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"hackers": hackers, "done_periods": sorted(list(done_periods))}, fh)
    os.replace(tmp, path)


def discover(country, start, end, delay, checkpoint_path, resume, only_period=None):
    hackers, done = (load_checkpoint(checkpoint_path) if resume else ({}, set()))
    if resume and hackers:
        print(f"resume: {len(hackers)} hacker, {len(done)} periode sudah beres", file=sys.stderr)

    periods = [(y, q) for y in range(start, end + 1) for q in (None, 1, 2, 3, 4)]
    if only_period:
        y, q = only_period
        periods = [(y, q)]

    for year, quarter in periods:
        key = f"{year}-{quarter or 'annual'}"
        if key in done:
            continue
        label = f"{year}" + (f" Q{quarter}" if quarter else " annual")
        before = len(hackers)
        for engagement, gkey in ENGAGEMENTS:
            for user_type in USER_TYPES:
                base = {
                    "key": gkey, "year": year, "quarter": quarter,
                    "filter": country, "user_type": user_type,
                    "engagement_type": engagement,
                }
                try:
                    slice_n = 0
                    for user in sweep(base, delay):
                        if user.get("country") and user["country"] != country:
                            continue
                        hackers[user["username"]] = {**user, "user_type": user_type}
                        slice_n += 1
                    if slice_n:
                        print(f"    {label} {engagement or 'all'}/{user_type}: {slice_n}", file=sys.stderr)
                except Exception as exc: 
                    print(f"  ! {label} {engagement}/{user_type}: {exc}", file=sys.stderr)
                time.sleep(delay)
        done.add(key)
        save_checkpoint(checkpoint_path, hackers, done)
        print(f"{label}: +{len(hackers) - before} baru (total {len(hackers)})", file=sys.stderr)
    return hackers


def rank(hackers):
    scored = [u for u in hackers.values() if (u.get("reputation") or 0) > 0]
    dropped = len(hackers) - len(scored)
    scored.sort(key=lambda u: (-u["reputation"], u.get("id") or "", u["username"]))
    rows = [
        {
            "country_rank": i, "username": u["username"], "reputation": u["reputation"],
            "worldwide_rank": u.get("rank"), "resolved_reports": u.get("resolved_report_count"),
            "thanks_items": u.get("thanks_items_total_count"),
            "signal": None if u.get("signal") is None else round(u["signal"], 2),
            "impact": None if u.get("impact") is None else round(u["impact"], 2),
            "user_type": u["user_type"], "profile": f"https://hackerone.com/{u['username']}",
            "user_id": u.get("id"),
        }
        for i, u in enumerate(scored, 1)
    ]
    return rows, dropped


def existing_row_count(path):
    if not os.path.exists(path):
        return 0
    with open(path, newline="") as fh:
        return sum(1 for _ in csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="ID")
    ap.add_argument("--start", type=int, default=2020)
    ap.add_argument("--end", type=int, default=datetime.now(timezone.utc).year)
    ap.add_argument("--delay", type=float, default=0.25)
    ap.add_argument("--out-dir", default="data")
    ap.add_argument("--checkpoint", default=None, help="default: <out-dir>/.checkpoint_<country>.json")
    ap.add_argument("--resume", action="store_true", help="lanjutkan dari checkpoint terakhir")
    ap.add_argument("--only-period", default=None, help='mis. "2025" atau "2025Q3" untuk sapu ulang satu periode')
    ap.add_argument("--max-shrink", type=float, default=0.15)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    checkpoint_path = args.checkpoint or os.path.join(args.out_dir, f".checkpoint_{args.country}.json")

    only_period = None
    if args.only_period:
        s = args.only_period
        if "Q" in s:
            y, q = s.split("Q")
            only_period = (int(y), int(q))
        else:
            only_period = (int(s), None)

    hackers = discover(args.country, args.start, args.end, args.delay,
                        checkpoint_path, args.resume, only_period)
    rows, dropped = rank(hackers)
    if not rows:
        sys.exit("tidak ada hacker ditemukan - menolak menulis file kosong")

    print(f"\n{len(rows)} ranked, {dropped} dropped (tanpa reputasi)", file=sys.stderr)

    path = os.path.join(args.out_dir, f"leaderboard_{args.country}.csv")
    previous = existing_row_count(path)
    if previous:
        floor = previous * (1 - args.max_shrink)
        if len(rows) < floor:
            sys.exit(f"menolak menulis: {len(rows)} baris vs {previous} sebelumnya "
                      f"(floor {floor:.0f}). Kemungkinan scrape parsial - jalankan ulang dengan --resume.")

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {path} ({previous} -> {len(rows)} rows)", file=sys.stderr)

    meta_path = os.path.join(args.out_dir, f"meta_{args.country}.json")
    with open(meta_path, "w") as fh:
        json.dump({
            "country": args.country,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ranked": len(rows), "discovered": len(hackers), "dropped_no_reputation": dropped,
            "years_swept": [args.start, args.end],
        }, fh, indent=2)
        fh.write("\n")

    if not only_period and os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)

    for r in rows[:10]:
        print(f"{r['country_rank']:>4}  {r['username']:<24} {r['reputation']:>8} pts  WW #{r['worldwide_rank']}")


if __name__ == "__main__":
    main()