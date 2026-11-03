#!/usr/bin/env python3
"""Profile analyser: update a "recent activity" section of a GitHub profile README
from the owner's most recently active public repos.

It reads only the last N repos (default 5) that had pushes, and writes only the
profile README, between these two markers:

    <!-- analyser:start -->
    <!-- analyser:end -->

Usage:
    github_analyser.py --user NAME [--count 5] [--days 30] [--exclude a,b]
                       [--readme README.md] [--fixture bundle.json] [--title TEXT]

    --readme   rewrite that file between the markers; without it, print the section
    --fixture  read saved API data instead of calling GitHub (offline tests)

Live mode calls api.github.com, using $GITHUB_TOKEN if set. Python 3 standard
library only, so the workflow installs nothing.

Commit messages and descriptions are untrusted input: anyone whose PR you merge
can write them. Everything shown is cut to its first line, stripped of HTML,
bare URLs and control characters, truncated, and Markdown-escaped. Only commits
authored by the profile owner count; merges, bots and wip/fixup commits are skipped.
Output uses fixed dates, never "3 days ago", so a run with no new activity
produces no change and no commit.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

START, END = "<!-- analyser:start -->", "<!-- analyser:end -->"
API = "https://api.github.com"
SKIP_MESSAGE = re.compile(r"^(merge (pull request|branch|remote-tracking)|wip\b|fixup!|squash!|amend!)", re.I)


# ---------------------------------------------------------------- data sources
def fetch_live(user, count, days, exclude):
    token = os.environ.get("GITHUB_TOKEN")

    def get(path, **params):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-analyser",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = get(f"/users/{user}/repos", per_page=100, sort="pushed")
    detail = {}
    for repo in pick_repos(repos, user, count, exclude):
        name = repo["name"]
        detail[name] = {
            "commits": get(f"/repos/{user}/{name}/commits", since=since, per_page=100),
            "releases": get(f"/repos/{user}/{name}/releases", per_page=5),
        }
    return {"now": now.isoformat(), "repos": repos, "detail": detail}


def load_fixture(path):
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- analysis
def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pick_repos(repos, user, count, exclude):
    keep = [r for r in repos
            if not r.get("fork") and not r.get("archived") and not r.get("private")
            and r["name"].lower() != user.lower() and r["name"] not in exclude]
    keep.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    return keep[:count]


def own_commits(commits, user, since):
    """The owner's meaningful commits since `since`, newest first."""
    out = []
    for c in commits:
        author = c.get("author") or {}
        msg = (c.get("commit", {}).get("message") or "").strip()
        when = parse_time(c["commit"]["author"]["date"])
        if (author.get("login", "").lower() != user.lower() or author.get("type") == "Bot"
                or len(c.get("parents", [])) > 1 or SKIP_MESSAGE.match(msg) or when < since):
            continue
        out.append({"msg": msg, "url": c.get("html_url", ""), "when": when})
    out.sort(key=lambda c: c["when"], reverse=True)
    return out


def safe(text, limit=70):
    """Make untrusted text inert for a Markdown table cell."""
    text = (text or "").splitlines()[0] if text else ""
    text = re.sub(r"<[^>]*>", "", text)               # HTML tags, incl. <img> trackers
    text = re.sub(r"https?://\S+|www\.\S+", "[link]", text)  # no outbound links from commit text
    text = re.sub(r"[\x00-\x1f\x7f​-‏‪-‮]", "", text)  # control + bidi chars
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = re.sub(r"([\\`*_{}\[\]()#+!|<>~])", r"\\\1", text)  # Markdown-significant characters
    return text.replace("@", "@⁠")               # no accidental @mention links


def safe_url(url, user):
    """Only links into the owner's own repos on github.com."""
    prefix = f"https://github.com/{user}/"
    return url if isinstance(url, str) and url.lower().startswith(prefix.lower()) and '"' not in url else ""


def status(when, now):
    if when is None:
        return "⚪"
    age = (now - when).days
    return "🟢" if age <= 7 else "🟡" if age <= 30 else "⚪"


def build_section(data, user, count=5, days=30, exclude=(), title="🔭 Recent activity"):
    now = parse_time(data["now"])
    since = now - timedelta(days=days)
    repos = pick_repos(data["repos"], user, count, set(exclude))
    rows, total, langs, shipped = [], 0, Counter(), []

    for repo in repos:
        info = data["detail"].get(repo["name"], {})
        commits = own_commits(info.get("commits", []), user, since)
        total += len(commits)
        if repo.get("language") and commits:
            langs[repo["language"]] += len(commits)
        for rel in info.get("releases", []):
            if not rel.get("draft") and rel.get("published_at") and parse_time(rel["published_at"]) >= since:
                shipped.append((parse_time(rel["published_at"]), repo, rel))

        name = safe(repo["name"], 40)
        url = safe_url(repo.get("html_url"), user)
        cell = f"[**{name}**]({url})" if url else f"**{name}**"
        if repo.get("language"):
            cell += f"<br><sub>{safe(repo['language'], 20)}</sub>"
        if commits:
            last = commits[0]
            curl = safe_url(last["url"], user)
            change = f"[{safe(last['msg'])}]({curl})" if curl else safe(last["msg"])
            if len(commits) > 1:
                change += f"<br><sub>+{len(commits) - 1} more in {days} days</sub>"
            when = last["when"]
        else:
            change = f"<sub>no commits in the last {days} days</sub>"
            when = parse_time(repo["pushed_at"]) if repo.get("pushed_at") else None
        date = f"{when.day} {when:%b %Y}" if when else "–"
        rows.append(f"| {status(when, now)} | {cell} | {change} | {date} |")

    lines = [START, f"### {title}", "", "| | Repo | Latest change | Date |", "| --- | --- | --- | --- |", *rows, ""]
    if shipped:
        shipped.sort(key=lambda s: s[0], reverse=True)
        items = []
        for when, repo, rel in shipped[:3]:
            label = safe(rel.get("name") or rel.get("tag_name"), 40)
            rurl = safe_url(rel.get("html_url"), user)
            text = f"[{safe(repo['name'], 40)} {label}]({rurl})" if rurl else f"{safe(repo['name'], 40)} {label}"
            items.append(f"{text} ({when.day} {when:%b})")
        lines += [f"**Recently shipped:** {' · '.join(items)}", ""]
    mostly = f", mostly {safe(langs.most_common(1)[0][0], 20)}" if langs else ""
    active = sum(1 for r in rows if r.startswith("| 🟢") or r.startswith("| 🟡"))
    lines.append(f"<sub>Last {days} days: {total} commit{'s' * (total != 1)} across {active} "
                 f"repo{'s' * (active != 1)}{mostly}. 🟢 this week · 🟡 this month · ⚪ quieter. "
                 f"Updated automatically from my {len(repos)} most recently active public repos.</sub>")
    lines.append(END)
    out = "\n".join(lines)
    if IMAGE.search(out):
        sys.exit("analyser: refusing to write: the section would contain an image")
    return out


# The analyser never adds, removes or changes images: not in its own section,
# and nowhere else (it only ever rewrites the text between the markers).
IMAGE = re.compile(r"<\s*(img|picture|source|svg|video)\b|!\[", re.I)


def split_markers(text, where="README"):
    if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
        sys.exit(f"analyser: {where} needs exactly one {START} ... {END} block. Add the markers first.")
    head, rest = text.split(START, 1)
    return head, rest.split(END, 1)[1]


def outside(text):
    """Everything outside the marker block. The workflow compares this before and
    after a run, so any change to the banner, images or other sections fails the job."""
    head, tail = split_markers(text)
    return head + START + END + tail


def update_readme(path, section):
    if os.path.basename(path) != "README.md":
        sys.exit(f"analyser: only README.md may be written, not {path}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    head, tail = split_markers(text, path)
    new = head + section + tail
    if outside(new) != outside(text):  # can't happen by construction; belt and braces
        sys.exit("analyser: refusing to write: text outside the markers would change")
    changed = new != text
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
    return changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--user")
    ap.add_argument("--outside", action="store_true",
                    help="read a README on stdin and print everything outside the markers (used by the workflow guard)")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--exclude", default="", help="comma-separated repo names to leave out")
    ap.add_argument("--readme")
    ap.add_argument("--fixture")
    ap.add_argument("--title", default="🔭 Recent activity")
    a = ap.parse_args()
    if a.outside:
        sys.stdout.write(outside(sys.stdin.read()))
        return
    if not a.user:
        ap.error("--user is required")
    exclude = {x.strip() for x in a.exclude.split(",") if x.strip()}

    data = load_fixture(a.fixture) if a.fixture else fetch_live(a.user, a.count, a.days, exclude)
    section = build_section(data, a.user, a.count, a.days, exclude, a.title)

    if not a.readme:
        print(section)
        return
    changed = update_readme(a.readme, section)
    print("analyser: README updated" if changed else "analyser: no change")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
