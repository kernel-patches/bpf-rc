"""Select production CI failures and collect evidence for rc triage."""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from github import Auth, Github


FAILURES = {"failure", "timed_out"}
MAX_RUNS = 10


def write_json(path, data, indent=2):
    Path(path).write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")


def failed_series(run):
    return (
        run.path == ".github/workflows/test.yml"
        and run.event == "pull_request"
        and run.status == "completed"
        and run.conclusion in FAILURES
        and run.head_branch.startswith("series/")
    )


def already_recorded(rc, run):
    for state in ("done", "failed"):
        name = f"ci-triage-{state}-{run.id}-{run.run_attempt}"
        if any(artifact.expired is False for artifact in rc.get_artifacts(name=name).get_page(0)):
            return True
    return False


def discover(production, rc, run_id, attempt):
    if run_id is not None:
        run = production.get_workflow_run(run_id)
        if attempt is not None:
            run = run.get_attempt(attempt)
        candidates = [run]
    else:
        if attempt is not None:
            raise ValueError("Attempt requires a run ID")
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        workflow = production.get_workflow("test.yml")
        unique = {}
        for status in ("failure", "timed_out"):
            for run in workflow.get_runs(event="pull_request", status=status, created=f">{since}").get_page(0):
                unique[run.id, run.run_attempt] = run
        candidates = sorted(
            unique.values(), key=lambda run: (run.created_at, run.id, run.run_attempt), reverse=True
        )[:100]

    write_json("candidates.json", {"workflow_runs": [run.raw_data for run in candidates]})
    selected = []
    for run in candidates:
        if not failed_series(run):
            continue
        if run_id is None and already_recorded(rc, run):
            print(f"Skipping recorded run {run.id} attempt {run.run_attempt}", file=sys.stderr)
            continue
        twin = next(iter(rc.get_pulls(state="all", head=f"kernel-patches:{run.head_branch}")), None)
        if twin is None:
            print(f"No rc twin for {run.id} ({run.head_branch})", file=sys.stderr)
            continue
        selected.append(
            {"run": str(run.id), "attempt": str(run.run_attempt), "pr": twin.number, "branch": run.head_branch}
        )
        if len(selected) == MAX_RUNS:
            break

    write_json("selection.json", selected, indent=None)
    print(f"Selected {len(selected)} production runs", file=sys.stderr)
    if run_id is not None and len(selected) != 1:
        raise ValueError("Replay needs a completed failed test.yml series run with an rc twin")


def positive_id(value):
    if value == "":
        return None
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("Expected a positive decimal run ID or attempt")
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover",))
    parser.add_argument("--run-id", type=positive_id)
    parser.add_argument("--attempt", type=positive_id)
    args = parser.parse_args()
    with Github(auth=Auth.Token(os.environ["GH_TOKEN"]), per_page=100) as github:
        production = github.get_repo("kernel-patches/bpf")
        rc = github.get_repo("kernel-patches/bpf-rc")
        discover(production, rc, args.run_id, args.attempt)


if __name__ == "__main__":
    main()
