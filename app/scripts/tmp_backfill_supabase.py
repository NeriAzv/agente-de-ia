"""TEMPORARY backfill wrapper around sync_local_json_to_supabase.py.

This is a throwaway helper for the one-shot migration of local JSON state into
Supabase. It is intentionally not wired into anything and should be deleted
once the migration is validated. Production paths must not depend on it.

What it adds on top of `sync_local_json_to_supabase`:
- Loads `app/.env` automatically (no need to export vars in the shell).
- `--dry-run` mode that walks the same data path but performs ZERO network
  writes. Useful to inspect counts / row shapes before flipping the switch.
- `--apply` mode that performs the real upserts.
- Defaults to dry-run if no flag is passed (defensive).
- Prints row counts per table before and after.
- Catches and logs per-record errors without aborting the whole run, except
  for credential / schema-level errors (4xx/5xx that affect the entire batch),
  which bubble up immediately.

Usage:
    python app/scripts/tmp_backfill_supabase.py --dry-run
    python app/scripts/tmp_backfill_supabase.py --apply

Does NOT modify or delete any local JSON files.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "app" / ".env"
SCRIPTS_DIR = ROOT / "app" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def load_dotenv(path: Path) -> None:
    """Load `KEY=VALUE` lines from a .env file into os.environ.

    Inputs:
        path: Path to the .env file.

    Side effects:
        Sets os.environ entries for any KEY=VALUE pair found. Existing env
        vars are NOT overwritten (shell-level wins).

    Returns:
        None.

    Notes:
        Lines starting with `#` and blank lines are ignored. Values are not
        stripped of surrounding quotes (the project's .env has none).
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


load_dotenv(ENV_FILE)

import sync_local_json_to_supabase as sync  # noqa: E402  (import after env load)


TABLES = ("conversations", "lead_profiles", "messages", "followup_jobs", "meetings")


class DryRunClient:
    """Stand-in for sync.SupabaseREST that records calls without sending them.

    Inputs (constructor):
        None.

    Side effects:
        None. All "writes" are kept in-memory in `self.calls`.

    Notes:
        Returns synthetic rows with deterministic UUID-shaped placeholders so
        downstream code (which reads `conv[0]["id"]`) keeps working.
    """

    def __init__(self) -> None:
        self.calls: Dict[str, int] = {t: 0 for t in TABLES}
        self._fake_id_seq = 0

    def upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> List[Dict[str, Any]]:
        """Simulate an upsert: count rows, return synthetic ids, never call the network.

        Inputs:
            table: Target table name.
            rows: Payload that would be upserted.
            on_conflict: Conflict columns (unused in dry-run).

        Returns:
            A list mirroring `rows` length where the first item carries a
            synthetic `id` so the caller's `conv[0]["id"]` access succeeds.
        """
        n = len(rows)
        self.calls[table] = self.calls.get(table, 0) + n
        if not rows:
            return []
        self._fake_id_seq += 1
        synthetic = [{"id": f"dryrun-{self._fake_id_seq:08d}", **r} for r in rows]
        return synthetic


class SafeClient:
    """Wraps sync.SupabaseREST and downgrades per-record failures to warnings.

    Inputs (constructor):
        real: An initialized sync.SupabaseREST instance.

    Side effects:
        Performs HTTP POSTs. On a 4xx/5xx, retries the batch one row at a time
        so a single bad payload doesn't sink the whole sync. Credential-level
        failures (401/403) are raised unchanged.

    Returns:
        Same shape as sync.SupabaseREST.upsert (list of representation rows).
    """

    def __init__(self, real: sync.SupabaseREST) -> None:
        self.real = real
        self.errors: List[str] = []

    def upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> List[Dict[str, Any]]:
        """Try the bulk upsert; on batch failure, fall back to per-row upserts.

        Inputs:
            table: Target Supabase table.
            rows: Rows to upsert.
            on_conflict: Conflict columns string.

        Returns:
            List of representation rows for successful inserts/updates. Failed
            rows are skipped and their error is recorded in `self.errors`.

        Raises:
            requests.HTTPError for 401/403 (credential failure) — these abort
            the whole run because retrying per-row would not help.
        """
        if not rows:
            return []
        try:
            return self.real.upsert(table, rows, on_conflict)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (401, 403):
                raise
            print(f"[warn] batch upsert into {table} failed ({status}); "
                  f"retrying row-by-row…")
            out: List[Dict[str, Any]] = []
            for r in rows:
                try:
                    res = self.real.upsert(table, [r], on_conflict)
                    out.extend(res)
                except requests.HTTPError as inner:
                    s = inner.response.status_code if inner.response is not None else 0
                    if s in (401, 403):
                        raise
                    body = inner.response.text if inner.response is not None else str(inner)
                    self.errors.append(f"{table}: {body[:200]}")
                    print(f"[warn] skipping bad row in {table}: {body[:200]}")
            return out


def fetch_counts(url: str, key: str) -> Dict[str, Optional[int]]:
    """Return current row count per backfilled table via PostgREST HEAD+count.

    Inputs:
        url: Supabase project URL (no trailing slash).
        key: Service-role key.

    Returns:
        Dict mapping table name to count, or None when the request failed.

    Side effects:
        Issues one HEAD request per table.
    """
    counts: Dict[str, Optional[int]] = {}
    for t in TABLES:
        try:
            r = requests.head(
                f"{url}/rest/v1/{t}",
                params={"select": "id"},
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Prefer": "count=exact",
                    "Range-Unit": "items",
                    "Range": "0-0",
                },
                timeout=30,
            )
            rng = r.headers.get("Content-Range") or ""
            total = rng.split("/")[-1]
            counts[t] = int(total) if total.isdigit() else None
        except Exception:
            counts[t] = None
    return counts


def print_counts(label: str, counts: Dict[str, Optional[int]]) -> None:
    """Pretty-print a counts dict under a label.

    Inputs:
        label: Header text (e.g. "Before").
        counts: Mapping table -> count or None.

    Side effects:
        Writes to stdout.

    Returns:
        None.
    """
    print(f"\n=== {label} ===")
    for t in TABLES:
        v = counts.get(t)
        print(f"  {t:<16} {('?' if v is None else v):>8}")


def run(mode: str) -> int:
    """Entry point for both dry-run and apply modes.

    Inputs:
        mode: Either "dry-run" or "apply".

    Side effects:
        - dry-run: reads local JSON, walks the sync logic, prints planned counts.
        - apply:   issues HTTP POSTs to Supabase to upsert rows.
        - Both:   prints before/after counts (after only in apply mode).

    Returns:
        Process exit code: 0 on success, non-zero on credential/schema failure.
    """
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in app/.env")
        return 2

    print(f"Mode: {mode}")
    print(f"Project URL: {url}")

    before = fetch_counts(url, key)
    print_counts("Before", before)

    if mode == "dry-run":
        client = DryRunClient()
        sync.SUPABASE_URL = url
        sync.SUPABASE_SERVICE_ROLE_KEY = key
        original_cls = sync.SupabaseREST
        sync.SupabaseREST = lambda *_a, **_k: client  # type: ignore[assignment]
        try:
            sync.main()
        finally:
            sync.SupabaseREST = original_cls
        print("\n=== Planned upsert volume (dry-run) ===")
        for t in TABLES:
            print(f"  {t:<16} {client.calls.get(t, 0):>8}")
        print("\nNo writes performed. Re-run with --apply to commit.")
        return 0

    real = sync.SupabaseREST(url, key)
    safe = SafeClient(real)
    sync.SUPABASE_URL = url
    sync.SUPABASE_SERVICE_ROLE_KEY = key
    original_cls = sync.SupabaseREST
    sync.SupabaseREST = lambda *_a, **_k: safe  # type: ignore[assignment]
    try:
        sync.main()
    finally:
        sync.SupabaseREST = original_cls

    after = fetch_counts(url, key)
    print_counts("After", after)

    print("\n=== Delta ===")
    for t in TABLES:
        b = before.get(t)
        a = after.get(t)
        delta = (a - b) if (a is not None and b is not None) else None
        print(f"  {t:<16} {('?' if b is None else b):>6} -> "
              f"{('?' if a is None else a):>6}   "
              f"(delta {'?' if delta is None else delta:+d})")

    if safe.errors:
        print(f"\n[warn] {len(safe.errors)} per-row errors occurred (see [warn] lines above).")
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    """Parse CLI flags: --dry-run / --apply.

    Inputs:
        argv: Arguments without the program name.

    Returns:
        Namespace with `mode` attribute set to "dry-run" or "apply".

    Notes:
        Defaults to dry-run when neither flag is provided.
    """
    p = argparse.ArgumentParser(description="Throwaway backfill of local JSON -> Supabase.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Walk the data path without writing.")
    g.add_argument("--apply", action="store_true", help="Perform real upserts.")
    ns = p.parse_args(argv)
    ns.mode = "apply" if ns.apply else "dry-run"
    return ns


def main() -> int:
    """Top-level CLI dispatcher.

    Returns:
        Process exit code.
    """
    ns = parse_args(sys.argv[1:])
    return run(ns.mode)


if __name__ == "__main__":
    raise SystemExit(main())
