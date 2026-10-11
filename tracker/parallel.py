"""Reads several sources at once.

Most sources answer in a second or two, but a slow government site can take minutes to give
up (CERT-In: two tries of 100 seconds each). Read one after another, a few slow sites in one
run add up to the step's time limit; read side by side, a run takes about as long as its
slowest site. Each reader still gets its own retries and time-outs.

Switch: "parallel_reads" in watchlist.yml, how many sites at once; 1 = one after another, as
before. Lines a reader prints while it works can arrive mixed together; each source's summary
line is printed afterwards, in the usual order.
"""

import pathlib
from concurrent.futures import ThreadPoolExecutor

import yaml

WATCHLIST = pathlib.Path(__file__).resolve().parent.parent / "watchlist.yml"


def workers() -> int:
    try:
        n = (yaml.safe_load(WATCHLIST.read_text()) or {}).get("parallel_reads", 1)
        return max(1, min(int(n), 12))
    except (OSError, yaml.YAMLError, TypeError, ValueError):
        return 1


def run(jobs: list) -> list:
    """jobs: [(name, reader)], reader taking no arguments. Returns [(name, found, error)] in
    the same order; error is None when the reader worked, found is None when it did not."""
    def one(job):
        name, read = job
        try:
            return name, read(), None
        except Exception as exc:                                      # noqa: BLE001
            return name, None, exc

    n = workers()
    if n == 1 or len(jobs) < 2:
        return [one(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(one, jobs))
