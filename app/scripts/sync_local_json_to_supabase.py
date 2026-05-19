"""Backfill local chat JSON files into Supabase.

This script is intended to be executed manually as a one-shot backfill that
moves the existing local state (`chats/{chatLid}/history.json`,
`chats/{chatLid}/lead_info.json`, `reunioes.json`, `app/reunioes.json`) into the
Supabase tables `conversations`, `lead_profiles`, `messages`, `followup_jobs`
and `meetings`.

Idempotency
-----------
Every write is an UPSERT on a deterministic conflict key, so the script can be
re-run safely:

- conversations:  on_conflict=chat_lid
- lead_profiles:  on_conflict=conversation_id
- messages:       on_conflict=(conversation_id, message_id)
- followup_jobs:  on_conflict=(conversation_id, tipo, target_at)
- meetings:       on_conflict=(conversation_id, scheduled_for)

Because Postgres treats multiple NULLs in a UNIQUE index as distinct, any
message without a Z-API `messageId` would duplicate on re-run. To prevent that
this script derives a deterministic `message_id` (`local:<sha1>`) from
`chat_lid|event_ts|direction|content_text` whenever the original is absent.

Timezone policy
---------------
History events use a unix `timestamp` field which is timezone-agnostic and is
converted to UTC unambiguously. All other timestamps in the JSON files
(`atualizado_em`, `horario_iso`, `agendado_em`, `datetime`) are produced by
`datetime.now().isoformat()` on the bot host, which runs in Brazil. They are
therefore interpreted as naive local time in `America/Sao_Paulo` and converted
to UTC before being stored. Already-tz-aware ISO strings are passed through
unchanged.

Status inference (backfill heuristics)
--------------------------------------
The local JSON does not record terminal states. To avoid the previous bug of
hardcoding every follow-up/meeting as "scheduled", this script infers status
from `target_at` / `scheduled_for` at backfill time:

- followup_jobs: future -> 'scheduled'; past -> 'failed' (likely missed before
  the migration; will surface in dashboards instead of silently looking pending).
- meetings: future -> 'scheduled'; past -> 'completed' (best-effort default;
  manual correction in Supabase if a past meeting was cancelled).

The runtime (once migrated) will own all subsequent transitions explicitly.

Usage
-----
Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in the environment
(see `app/.env`) and run:

    python app/scripts/sync_local_json_to_supabase.py

The script does NOT delete or modify any local JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[2]
CHATS_DIR = ROOT / "chats"
REUNIOES_FILES = [ROOT / "reunioes.json", ROOT / "app" / "reunioes.json"]

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

LOCAL_TZ = ZoneInfo("America/Sao_Paulo") if ZoneInfo else timezone.utc
VALID_FOLLOWUP_TIPOS = {"1h", "24h", "15d"}


class SupabaseREST:
    """Thin REST client for Supabase PostgREST endpoints.

    Inputs:
        url: Supabase project URL (no trailing slash).
        key: Service-role key. Bypasses RLS.

    Side effects:
        Network POSTs to `<url>/rest/v1/<table>` for upserts and inserts.

    Raises:
        RuntimeError if credentials are missing.
        requests.HTTPError if Supabase returns a non-2xx response.
    """

    def __init__(self, url: str, key: str):
        if not url or not key:
            raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in environment.")
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def upsert(self, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> List[Dict[str, Any]]:
        """Upsert rows merging duplicates on the given conflict key(s).

        Inputs:
            table: Supabase table name (e.g. "conversations").
            rows: List of JSON-serializable dicts. Empty list returns [].
            on_conflict: Comma-separated unique-constraint columns.

        Returns:
            The representation of inserted/updated rows (PostgREST response).

        Side effects:
            HTTP POST with `Prefer: resolution=merge-duplicates`.
        """
        if not rows:
            return []
        params = {"on_conflict": on_conflict}
        h = dict(self.headers)
        h["Prefer"] = "resolution=merge-duplicates,return=representation"
        r = requests.post(
            f"{self.base}/{table}",
            params=params,
            headers=h,
            data=json.dumps(rows),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()


def iso_from_epoch(ts: Any) -> Optional[str]:
    """Convert a unix epoch (seconds, possibly float) to an ISO-8601 UTC string.

    Inputs:
        ts: A number or numeric string representing seconds since the epoch.

    Returns:
        ISO-8601 string with `+00:00` offset, or None on failure.

    Notes:
        Used for history.json `timestamp` fields, which are absolute and
        therefore unambiguous.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def parse_iso(s: Any) -> Optional[str]:
    """Normalize a local-or-aware ISO timestamp string to UTC ISO-8601.

    Inputs:
        s: Any value; only non-empty strings are processed.

    Returns:
        ISO-8601 UTC string, or None when the input cannot be parsed.

    Behavior:
        Naive datetimes (no tzinfo) are assumed to be in America/Sao_Paulo,
        because every JSON producer in this repo runs on the Brazil host and
        writes `datetime.now().isoformat()` without tz attached. They are
        localized then converted to UTC. Already-aware datetimes are converted
        to UTC unchanged. This fixes the previous bug where naive timestamps
        were tagged as UTC, introducing a 3h offset.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc).isoformat()


def infer_message_type(data: Dict[str, Any]) -> str:
    """Return a short label for the message media type.

    Inputs:
        data: Z-API `data` payload dict from history.json.

    Returns:
        One of "text", "audio", "image", "video", "document", the value of
        `data["type"]`, or "unknown" as a last resort.
    """
    for k in ("text", "audio", "image", "video", "document"):
        if isinstance(data.get(k), dict):
            return k
    return data.get("type") or "unknown"


def extract_content_text(data: Dict[str, Any]) -> str:
    """Extract a best-effort textual representation of a message payload.

    Inputs:
        data: Z-API `data` payload dict.

    Returns:
        A string (possibly empty) containing the message text, the media
        transcription/description/caption, or "" when nothing is available.
    """
    text = data.get("text")
    if isinstance(text, dict):
        return text.get("message") or ""
    if isinstance(text, str):
        return text
    for media_key in ("audio", "image", "video", "document"):
        media = data.get(media_key)
        if isinstance(media, dict):
            return (
                media.get("transcricao")
                or media.get("descricao")
                or media.get("conteudo")
                or media.get("caption")
                or ""
            )
    return ""


def stable_message_id(chat_lid: str, event_ts: str, direction: str, content_text: str) -> str:
    """Build a deterministic synthetic message_id for events without one.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        event_ts: ISO timestamp of the event (already normalized).
        direction: "inbound" | "outbound" | "system".
        content_text: Best-effort textual content (may be empty).

    Returns:
        A string of the form "local:<sha1>" — stable across runs for the same
        input tuple, used as fallback in the unique(conversation_id, message_id)
        index when Z-API did not provide a messageId.

    Notes:
        SHA-1 is used for compactness, not for cryptographic strength.
    """
    raw = f"{chat_lid}|{event_ts}|{direction}|{content_text}".encode("utf-8")
    return f"local:{hashlib.sha1(raw).hexdigest()}"


def load_json(path: Path, default: Any) -> Any:
    """Read a JSON file, returning `default` on absence or parse error.

    Inputs:
        path: Filesystem path to the JSON file.
        default: Value to return when the file is missing or malformed.

    Returns:
        The parsed JSON content, or `default`.
    """
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def merge_meetings(files: Iterable[Path]) -> List[Dict[str, Any]]:
    """Union meeting JSON files and dedupe by (chatLid, datetime).

    Inputs:
        files: Iterable of paths to JSON arrays of meeting dicts.

    Returns:
        A list with at most one entry per (chatLid, datetime). Entries without
        both fields are dropped. Earlier files win on conflict.

    Notes:
        The repo has two `reunioes.json` files (root and `app/`) that are both
        legitimate sources but partially overlap. This dedup avoids inserting
        the same meeting twice when running the backfill.
    """
    seen: Dict[tuple, Dict[str, Any]] = {}
    for f in files:
        data = load_json(f, [])
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict):
                continue
            chat_lid = r.get("chatLid")
            dt = r.get("datetime")
            if not chat_lid or not dt:
                continue
            key = (chat_lid, dt)
            if key not in seen:
                seen[key] = r
    return list(seen.values())


def followup_status_for(target_at_iso: str, now_utc: datetime) -> str:
    """Infer the followup status from its UTC target timestamp.

    Inputs:
        target_at_iso: Normalized UTC ISO string of the followup target.
        now_utc: Current UTC datetime, passed in for testability.

    Returns:
        "scheduled" when the target is in the future, "failed" otherwise.

    Notes:
        Local JSON only stores pending followups; sent/cancelled entries are
        removed by the runtime. Anything still present whose target_at has
        passed is therefore a leftover the bot did not process — surfaced as
        "failed" rather than silently kept as "scheduled".
    """
    try:
        target_dt = datetime.fromisoformat(target_at_iso)
    except Exception:
        return "failed"
    return "scheduled" if target_dt > now_utc else "failed"


def meeting_status_for(scheduled_for_iso: str, now_utc: datetime) -> str:
    """Infer the meeting status from its UTC scheduled timestamp.

    Inputs:
        scheduled_for_iso: Normalized UTC ISO string.
        now_utc: Current UTC datetime.

    Returns:
        "scheduled" if the meeting is in the future, "completed" otherwise.

    Notes:
        Best-effort default; a manual override is possible in Supabase for
        meetings that were actually cancelled.
    """
    try:
        scheduled_dt = datetime.fromisoformat(scheduled_for_iso)
    except Exception:
        return "scheduled"
    return "scheduled" if scheduled_dt > now_utc else "completed"


def main() -> None:
    """Run the full backfill: conversations, lead_profiles, messages, followups, meetings.

    Side effects:
        - Reads local JSON under `chats/` and the two `reunioes.json` files.
        - Writes to the Supabase tables listed at the top of this module.
        - Prints a per-chat progress line and a final meetings count to stdout.

    Returns:
        None.

    Raises:
        RuntimeError if Supabase credentials are missing.
        requests.HTTPError on Supabase non-2xx responses.
    """
    sb = SupabaseREST(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    conv_map: Dict[str, str] = {}
    now_utc = datetime.now(timezone.utc)

    if not CHATS_DIR.exists():
        print("No chats directory found. Nothing to sync.")
    else:
        for lead_dir in sorted(CHATS_DIR.iterdir()):
            if not lead_dir.is_dir():
                continue
            chat_lid = lead_dir.name
            lead_info = load_json(lead_dir / "lead_info.json", {}) or {}
            history = load_json(lead_dir / "history.json", []) or []

            phone = lead_info.get("phone")
            if not phone:
                for ev in history:
                    phone = (ev.get("data") or {}).get("phone")
                    if phone:
                        break

            ai_blocked = bool(lead_info.get("ai_blocked", False))
            conv = sb.upsert(
                "conversations",
                [{
                    "chat_lid": chat_lid,
                    "phone": phone,
                    "ai_blocked": ai_blocked,
                    "status": "blocked" if ai_blocked else "active",
                }],
                on_conflict="chat_lid",
            )
            conv_id = conv[0]["id"]
            conv_map[chat_lid] = conv_id

            profile = dict(lead_info)
            followups = profile.pop("followups_agendados", []) if isinstance(profile, dict) else []

            sb.upsert(
                "lead_profiles",
                [{
                    "conversation_id": conv_id,
                    "nome": profile.get("nome"),
                    "email": profile.get("email"),
                    "empresa": profile.get("empresa"),
                    "segmento_mercado": profile.get("segmento_mercado"),
                    "porte_empresa": profile.get("porte_empresa"),
                    "faturamento_aproximado": profile.get("faturamento_aproximado"),
                    "tamanho_time": profile.get("tamanho_time"),
                    "sistemas_atuais": profile.get("sistemas_atuais") or [],
                    "desafios_identificados": profile.get("desafios_identificados") or [],
                    "produto_indicado": profile.get("produto_indicado"),
                    "motivo_segmentacao": profile.get("motivo_segmentacao"),
                    "estagio_conversa": profile.get("estagio_conversa"),
                    "reuniao_agendada": profile.get("reuniao_agendada"),
                    "necessita_followup": profile.get("necessita_followup"),
                    "motivo_followup": profile.get("motivo_followup"),
                    "tentativas_retomada": int(profile.get("tentativas_retomada", 0) or 0),
                    "descricao_manual": profile.get("descricao_manual"),
                    "atualizado_em": parse_iso(profile.get("atualizado_em")),
                    "raw": profile,
                }],
                on_conflict="conversation_id",
            )

            followup_rows: List[Dict[str, Any]] = []
            for item in followups:
                tipo = item.get("tipo")
                if tipo not in VALID_FOLLOWUP_TIPOS:
                    continue
                target_at = parse_iso(item.get("horario_iso"))
                if not target_at:
                    continue
                followup_rows.append({
                    "conversation_id": conv_id,
                    "tipo": tipo,
                    "target_at": target_at,
                    "status": followup_status_for(target_at, now_utc),
                    "phone": item.get("phone") or phone,
                    "payload": item,
                })
            sb.upsert(
                "followup_jobs",
                followup_rows,
                on_conflict="conversation_id,tipo,target_at",
            )

            message_rows: List[Dict[str, Any]] = []
            for ev in history:
                data = ev.get("data") or {}
                from_me = bool(data.get("fromMe", False))
                direction = "outbound" if from_me else "inbound"
                event_ts = iso_from_epoch(ev.get("timestamp")) or now_utc.isoformat()
                content_text = extract_content_text(data)
                message_id = data.get("messageId") or stable_message_id(
                    chat_lid, event_ts, direction, content_text
                )
                message_rows.append({
                    "conversation_id": conv_id,
                    "event_ts": event_ts,
                    "direction": direction,
                    "sender_phone": data.get("phone") or phone,
                    "message_type": infer_message_type(data),
                    "content_text": content_text,
                    "message_id": message_id,
                    "reference_message_id": data.get("referenceMessageId"),
                    "payload": data,
                })

            chunk = 500
            for i in range(0, len(message_rows), chunk):
                sb.upsert(
                    "messages",
                    message_rows[i:i + chunk],
                    on_conflict="conversation_id,message_id",
                )

            print(f"Synced chat {chat_lid}: {len(message_rows)} messages, "
                  f"{len(followup_rows)} followups")

    meeting_rows: List[Dict[str, Any]] = []
    for r in merge_meetings(REUNIOES_FILES):
        chat_lid = r.get("chatLid")
        conv_id = conv_map.get(chat_lid)
        if not conv_id:
            conv = sb.upsert(
                "conversations",
                [{"chat_lid": chat_lid, "phone": r.get("phone")}],
                on_conflict="chat_lid",
            )
            conv_id = conv[0]["id"]
            conv_map[chat_lid] = conv_id
        scheduled_for = parse_iso(r.get("datetime"))
        if not scheduled_for:
            continue
        meeting_rows.append({
            "conversation_id": conv_id,
            "phone": r.get("phone"),
            "scheduled_for": scheduled_for,
            "agendado_em": parse_iso(r.get("agendado_em")),
            "meet_link": r.get("meet_link"),
            "event_id": r.get("event_id"),
            "status": meeting_status_for(scheduled_for, now_utc),
            "raw": r,
        })

    sb.upsert("meetings", meeting_rows, on_conflict="conversation_id,scheduled_for")
    print(f"Synced meetings: {len(meeting_rows)}")


if __name__ == "__main__":
    main()
