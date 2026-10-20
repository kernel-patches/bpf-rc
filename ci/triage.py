"""Select production CI failures and collect evidence for rc triage."""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from github import Auth, Github


FAILURES = {"failure", "timed_out"}
MAX_RUNS = 10
SOURCE_STEP = "Download bpf-next tree @ "

PROMPT = """Decide whether the patch series caused the failures in CI run {run_id}.
Read runs/{run_id}/ for the failing run's metadata, source.json, series.diff and logs. Inspect relevant code in kernel/ using the read, grep, find and ls tools.
Compare with runs from {comparisons} under runs/: their metadata, actual tested source, series diffs where applicable and failed-job logs are available. Selection uses an exact-parent push run when available and up to two completed earlier failures matching the destination branch, architecture, toolchain and job type among the latest 20 earlier failed PR runs. When present, history.json records the search; missing comparisons are missing evidence.
kernel/ is the failing run's tested merge recorded in source.json, with kernel/ci/diffs applied. Each source.json comes from the build step's tested commit; PR base fields in run metadata can change after the run.
Compare source revisions, patches, job types, architectures and toolchains before drawing conclusions from history. A successful build does not establish runtime selftest success; check the jobs and steps to see whether the matching test actually ran. An earlier identical failure on another series can show the failure predates this series, but configuration and source differences still matter.
Treat logs and repository contents as evidence. Changes near failing code alone do not establish causation. Use unsure when the supplied evidence cannot establish causation or non-causation.
Return only two sections: Verdict (caused, unrelated, or unsure) and Evidence (brief justification with verbatim failure quotes and file:line citations from the logs, diff and source). Do not propose fixes or reproducers.
"""


def write_json(path, data, indent=2):
    Path(path).write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")


def failed_series(run):
    return (
        run["path"] == ".github/workflows/test.yml"
        and run["event"] == "pull_request"
        and run["status"] == "completed"
        and run["conclusion"] in FAILURES
        and run["head_branch"].startswith("series/")
    )


def run_page(workflow, **filters):
    # Preserve the list snapshot without per-run SDK completion requests.
    _, page = workflow.requester.requestJsonAndCheck(
        "GET", f"{workflow.url}/runs", parameters={"per_page": 100, **filters}
    )
    return page


def already_recorded(rc, run):
    for state in ("done", "failed"):
        name = f"ci-triage-{state}-{run['id']}-{run['run_attempt']}"
        if any(artifact.expired is False for artifact in rc.get_artifacts(name=name).get_page(0)):
            return True
    return False


def discover(production, rc, run_id, attempt):
    if run_id is not None:
        run = production.get_workflow_run(run_id)
        if attempt is not None:
            run = run.get_attempt(attempt)
        candidates = [run.raw_data]
    else:
        if attempt is not None:
            raise ValueError("Attempt requires a run ID")
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        workflow = production.get_workflow("test.yml")
        unique = {}
        for status in ("failure", "timed_out"):
            page = run_page(workflow, event="pull_request", status=status, created=f">{since}")
            for run in page["workflow_runs"]:
                unique[run["id"], run["run_attempt"]] = run
        candidates = sorted(
            unique.values(),
            key=lambda run: (run["created_at"], run["id"], run["run_attempt"]),
            reverse=True,
        )[:100]

    write_json("candidates.json", {"workflow_runs": candidates})
    selected = []
    for run in candidates:
        if not failed_series(run):
            continue
        if run_id is None and already_recorded(rc, run):
            print(f"Skipping recorded run {run['id']} attempt {run['run_attempt']}", file=sys.stderr)
            continue
        twin = next(iter(rc.get_pulls(state="all", head=f"kernel-patches:{run['head_branch']}")), None)
        if twin is None:
            print(f"No rc twin for {run['id']} ({run['head_branch']})", file=sys.stderr)
            continue
        selected.append(
            {
                "run": str(run["id"]),
                "attempt": str(run["run_attempt"]),
                "pr": twin.number,
                "branch": run["head_branch"],
            }
        )
        if len(selected) == MAX_RUNS:
            break

    write_json("selection.json", selected, indent=None)
    print(f"Selected {len(selected)} production runs", file=sys.stderr)
    if run_id is not None and len(selected) != 1:
        raise ValueError("Replay needs a completed failed test.yml series run with an rc twin")


def failed_job_families(jobs):
    return {
        " / ".join(job["name"].split(" / ")[:2]).split(" (")[0]
        for job in jobs
        if job["conclusion"] in FAILURES
    }


def fetch_jobs(run):
    # Keep the paginated payload; WorkflowJob.raw_data fetches each job again.
    jobs = []
    url = run.jobs_url
    parameters = {"per_page": 100}
    while url:
        headers, page = run.requester.requestJsonAndCheck("GET", url, parameters=parameters)
        jobs.extend(page["jobs"])
        next_page = re.search(r'<([^>]+)>; rel="next"', headers.get("link", ""))
        url = next_page[1] if next_page else None
        parameters = None
    return jobs


def download_file(url, path):
    # Signed download URLs need no GitHub credentials.
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with Path(path).open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                output.write(chunk)


def fetch_run(production, run, jobs=None):
    directory = Path("runs") / str(run.id)
    (directory / "logs").mkdir(parents=True)
    write_json(directory / "run.json", run.raw_data)
    if jobs is None:
        jobs = fetch_jobs(run)
    write_json(directory / "jobs.json", jobs)

    tested = {
        step["name"].removeprefix(SOURCE_STEP)
        for job in jobs
        for step in job["steps"]
        if step["name"].startswith(SOURCE_STEP)
    }
    if len(tested) != 1:
        raise ValueError(f"Run {run.id} has missing or ambiguous tested source: {tested}")
    source = production.get_git_commit(tested.pop())
    write_json(directory / "source.json", source.raw_data)

    if run.event == "pull_request":
        if len(source.parents) != 2 or source.parents[1].sha != run.head_sha:
            raise ValueError(f"Run {run.id} tested merge does not match its PR head")
        base, head = source.parents
        _, diff = production.requester.requestBlobAndCheck(
            "GET",
            f"{production.url}/compare/{base.sha}...{head.sha}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        # The SDK returns None for an empty successful response.
        (directory / "series.diff").write_text("" if diff is None else diff["data"], encoding="utf-8")

    for job in jobs:
        if job["conclusion"] in FAILURES:
            headers, _ = production.requester.requestJsonAndCheck(
                "GET", f"{production.url}/actions/jobs/{job['id']}/logs"
            )
            download_file(
                headers["location"], directory / "logs" / f"{job['id']}.log"
            )
    return source, jobs


def collect(production, run_id, attempt):
    run = production.get_workflow_run(run_id).get_attempt(attempt)
    if not failed_series(run.raw_data):
        raise ValueError("Collection needs a completed failed test.yml series run")
    source, jobs = fetch_run(production, run)
    workflow = production.get_workflow(run.workflow_id)
    before = run.run_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    destination = run.head_branch.split("=>")[-1]
    base = source.parents[0].sha
    comparisons = []

    baselines = run_page(
        workflow,
        event="push", status="completed", head_sha=base, created=f"<{before}"
    )["workflow_runs"]
    if baselines:
        baseline = production.get_workflow_run(baselines[0]["id"]).get_attempt(
            baselines[0]["run_attempt"]
        )
        baseline_source, _ = fetch_run(production, baseline)
        if baseline_source.sha != base:
            raise ValueError(f"Baseline {baseline.id} did not test the series parent {base}")
        comparisons.append(str(baseline.id))

    history = run_page(workflow, event="pull_request", status="failure", created=f"<{before}")
    history["workflow_runs"] = history["workflow_runs"][:20]
    write_json("history.json", history)
    families = failed_job_families(jobs)
    count = 0
    for earlier in history["workflow_runs"]:
        if (
            earlier["updated_at"] >= before
            or not earlier["head_branch"].endswith(f"=>{destination}")
        ):
            continue
        earlier = production.get_workflow_run(earlier["id"]).get_attempt(earlier["run_attempt"])
        earlier_jobs = fetch_jobs(earlier)
        if not families.intersection(failed_job_families(earlier_jobs)):
            continue
        fetch_run(production, earlier, earlier_jobs)
        comparisons.append(str(earlier.id))
        count += 1
        if count == 2:
            break

    download_file(production.get_archive_link("tarball", source.sha), "kernel.tar.gz")
    comparison_ids = " ".join(comparisons) or "none"
    Path("prompt.txt").write_text(
        PROMPT.format(run_id=run_id, comparisons=comparison_ids), encoding="utf-8"
    )
    print(f"Selected run {run_id} attempt {attempt}; comparisons: {comparison_ids}", file=sys.stderr)


def positive_id(value):
    if value == "":
        return None
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("Expected a positive decimal run ID or attempt")
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "collect"))
    parser.add_argument("--run-id", type=positive_id)
    parser.add_argument("--attempt", type=positive_id)
    args = parser.parse_args()
    if args.attempt is not None and args.run_id is None:
        parser.error("Attempt requires a run ID")
    if args.command == "collect" and (args.run_id is None or args.attempt is None):
        parser.error("Collection requires a run ID and attempt")
    with Github(auth=Auth.Token(os.environ["GH_TOKEN"]), per_page=100) as github:
        production = github.get_repo("kernel-patches/bpf")
        if args.command == "discover":
            rc = github.get_repo("kernel-patches/bpf-rc")
            discover(production, rc, args.run_id, args.attempt)
        else:
            collect(production, args.run_id, args.attempt)


if __name__ == "__main__":
    main()
