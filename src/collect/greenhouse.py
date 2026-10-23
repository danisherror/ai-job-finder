"""Greenhouse adapter: reads a company's public job board feed."""

import sys

import httpx

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
HEADERS = {"User-Agent": "ai-job-finder/0.1 (private job digest)"}


def fetch(board: str, with_content: bool = False) -> list[dict]:
    """Return the raw job list for one Greenhouse board."""
    params = {"content": "true"} if with_content else None
    resp = httpx.get(API.format(board=board), params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()["jobs"]


if __name__ == "__main__":
    board = sys.argv[1] if len(sys.argv) > 1 else "groww"
    jobs = fetch(board)
    for job in jobs:
        print(f"{job['id']:>12}  {job['title'][:60]:<60}  {job['location']['name']}")
    print(f"\n{len(jobs)} jobs on Greenhouse board '{board}'")
