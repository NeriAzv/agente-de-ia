"""Single Supabase access point for the agent runtime.

Every read and write against the Supabase tables `conversations`,
`lead_profiles`, `messages`, `followup_jobs` and `meetings` MUST go through
this module. The rest of the codebase should not import `requests` or talk
to PostgREST directly.

Environment
-----------
Two env vars are required (typically loaded from `app/.env`):

    SUPABASE_URL                  e.g. https://<ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY     service-role JWT (server-side, bypasses RLS)

The first call into the module validates them and raises a clear
`RuntimeError` if either is missing. Credentials are never logged; HTTP
errors only surface the status code and PostgREST message body.

Idempotency model
-----------------
All writes use deterministic conflict keys to be safe on retry:

    conversations    on_conflict = chat_lid
    lead_profiles    on_conflict = conversation_id
    messages         on_conflict = (conversation_id, message_id)
    followup_jobs    on_conflict = (conversation_id, tipo, target_at)
    meetings         on_conflict = (conversation_id, scheduled_for)

Messages without an upstream `messageId` get a deterministic fallback id
`local:<sha1(chat_lid|event_ts|direction|content_text)>` so re-runs don't
duplicate them in the unique index (PostgreSQL treats multiple NULLs as
distinct in UNIQUE constraints).

Schema assumptions
------------------
This module assumes the schema defined in `supabase/schema.sql` is in place
in the target project. See the "Risks / assumptions" section at the bottom
of the module for the exact column expectations.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


LOCAL_TZ = ZoneInfo("America/Sao_Paulo") if ZoneInfo else timezone.utc

VALID_FOLLOWUP_TIPOS = ("1h", "24h", "15d")
VALID_FOLLOWUP_STATUSES = ("scheduled", "sent", "cancelled", "failed")
VALID_MEETING_STATUSES = ("scheduled", "cancelled", "completed")
VALID_CONV_STATUSES = ("active", "blocked", "closed")

LEAD_PROFILE_COLUMNS = (
    "nome",
    "email",
    "empresa",
    "segmento_mercado",
    "porte_empresa",
    "faturamento_aproximado",
    "tamanho_time",
    "sistemas_atuais",
    "desafios_identificados",
    "produto_indicado",
    "motivo_segmentacao",
    "estagio_conversa",
    "reuniao_agendada",
    "necessita_followup",
    "motivo_followup",
    "tentativas_retomada",
    "descricao_manual",
    "atualizado_em",
)


# ---------------------------------------------------------------------------
# HTTP client (lazy singleton)
# ---------------------------------------------------------------------------


class _Client:
    """Thin PostgREST wrapper. One instance per process.

    Inputs (constructor):
        url: Supabase project URL (trailing slash stripped).
        key: Service-role key.

    Side effects:
        Holds a `requests.Session` for connection pooling.

    Raises:
        RuntimeError if credentials are blank.
    """

    def __init__(self, url: str, key: str) -> None:
        if not url or not key:
            raise RuntimeError(
                "Supabase credentials missing: set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY in the environment."
            )
        self.base = f"{url.rstrip('/')}/rest/v1"
        self._key = key
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        """Issue an HTTP request and raise on non-2xx with a sanitized message.

        Inputs:
            method: "GET"/"POST"/"PATCH"/"DELETE".
            path: Path under `/rest/v1` (e.g. "/conversations").
            **kw: passed to requests (params/json/headers).

        Returns:
            The Response on 2xx.

        Raises:
            requests.HTTPError. The error message includes status and body
            but never the apikey.
        """
        url = f"{self.base}{path}"
        timeout = kw.pop("timeout", 30)
        r = self.session.request(method, url, timeout=timeout, **kw)
        if not r.ok:
            body = (r.text or "")[:500]
            raise requests.HTTPError(
                f"Supabase {method} {path} -> {r.status_code}: {body}",
                response=r,
            )
        return r

    def select(self, table: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """GET rows from `table` with PostgREST query params.

        Inputs:
            table: Table name.
            params: Query params (filter exprs, select, order, limit...).

        Returns:
            List of row dicts. Empty list when no match.
        """
        r = self._request("GET", f"/{table}", params=params)
        try:
            data = r.json()
            return data if isinstance(data, list) else []
        except ValueError:
            return []

    def upsert(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        on_conflict: str,
        ignore_duplicates: bool = False,
    ) -> List[Dict[str, Any]]:
        """POST rows with merge-duplicates semantics.

        Inputs:
            table: Table name.
            rows: Row dicts to upsert. Empty list is a no-op.
            on_conflict: Comma-separated unique-constraint columns.
            ignore_duplicates: If True, use `ignore-duplicates` instead of merge.

        Returns:
            Representation of inserted/updated rows.
        """
        if not rows:
            return []
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        headers = {
            "Prefer": f"resolution={resolution},return=representation",
        }
        r = self._request(
            "POST",
            f"/{table}",
            params={"on_conflict": on_conflict},
            headers=headers,
            data=json.dumps(rows),
        )
        try:
            return r.json() or []
        except ValueError:
            return []

    def patch(
        self,
        table: str,
        params: Dict[str, Any],
        patch: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """PATCH rows matched by `params` with `patch` body.

        Inputs:
            table: Table name.
            params: PostgREST filter expressions selecting target rows.
            patch: Column updates.

        Returns:
            Representation of updated rows.
        """
        headers = {"Prefer": "return=representation"}
        r = self._request(
            "PATCH",
            f"/{table}",
            params=params,
            headers=headers,
            data=json.dumps(patch),
        )
        try:
            return r.json() or []
        except ValueError:
            return []


_client_lock = threading.Lock()
_client_instance: Optional[_Client] = None
_conv_id_cache: Dict[str, str] = {}
_conv_cache_lock = threading.Lock()


def _client() -> _Client:
    """Return the process-wide _Client, creating it on first use.

    Returns:
        The shared _Client instance.

    Raises:
        RuntimeError when env vars are missing on first use.
    """
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = _Client(
                    os.environ.get("SUPABASE_URL", ""),
                    os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
                )
    return _client_instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Current UTC time as ISO-8601 string.

    Returns:
        ISO-8601 string with `+00:00` offset.
    """
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(ts: Any) -> Optional[str]:
    """Convert a unix epoch to ISO-8601 UTC. Returns None on failure."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _parse_iso(s: Any) -> Optional[str]:
    """Normalize a (possibly naive, possibly local) ISO string to UTC ISO.

    Inputs:
        s: A string. Anything else returns None.

    Returns:
        ISO-8601 UTC string, or None on parse failure.

    Notes:
        Naive datetimes are interpreted in America/Sao_Paulo (the bot host
        timezone), matching the historical convention used in local JSON.
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


def _infer_message_type(data: Dict[str, Any]) -> str:
    """Best-effort message media-type label."""
    for k in ("text", "audio", "image", "video", "document"):
        if isinstance(data.get(k), dict):
            return k
    return data.get("type") or "unknown"


def _extract_content_text(data: Dict[str, Any]) -> str:
    """Best-effort textual content from a Z-API data payload."""
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


def _stable_message_id(
    chat_lid: str, event_ts: str, direction: str, content_text: str
) -> str:
    """Deterministic synthetic id for messages without an upstream messageId.

    Returns:
        "local:<sha1>" stable for the same input tuple.
    """
    raw = f"{chat_lid}|{event_ts}|{direction}|{content_text}".encode("utf-8")
    return f"local:{hashlib.sha1(raw).hexdigest()}"


def _normalize_message(chat_lid: str, message: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a flexible message dict into a `messages` row payload.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        message: Either a Z-API event `{timestamp, data}` (history.json shape)
                 OR a pre-normalized dict with the message columns.

    Returns:
        Dict with the columns of `messages` (no conversation_id; the caller
        fills it). Always includes a deterministic `message_id`.
    """
    if "data" in message and isinstance(message["data"], dict):
        data = message["data"]
        from_me = bool(data.get("fromMe", False))
        direction = "outbound" if from_me else "inbound"
        event_ts = (
            _iso_from_epoch(message.get("timestamp"))
            or _parse_iso(message.get("event_ts"))
            or _now_iso()
        )
        content_text = _extract_content_text(data)
        message_id = data.get("messageId") or _stable_message_id(
            chat_lid, event_ts, direction, content_text
        )
        return {
            "event_ts": event_ts,
            "direction": direction,
            "sender_phone": data.get("phone"),
            "message_type": _infer_message_type(data),
            "content_text": content_text,
            "message_id": message_id,
            "reference_message_id": data.get("referenceMessageId"),
            "payload": data,
        }

    direction = message.get("direction") or "system"
    if direction not in ("inbound", "outbound", "system"):
        direction = "system"
    event_ts = _parse_iso(message.get("event_ts")) or _now_iso()
    content_text = message.get("content_text") or ""
    message_id = message.get("message_id") or _stable_message_id(
        chat_lid, event_ts, direction, content_text
    )
    return {
        "event_ts": event_ts,
        "direction": direction,
        "sender_phone": message.get("sender_phone"),
        "message_type": message.get("message_type") or "unknown",
        "content_text": content_text,
        "message_id": message_id,
        "reference_message_id": message.get("reference_message_id"),
        "payload": message.get("payload") or message,
    }


def _resolve_conversation_id(chat_lid: str, create_if_missing: bool = False) -> Optional[str]:
    """Return the conversation UUID for `chat_lid`, caching the lookup.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        create_if_missing: When True, insert an empty conversation row if none
            exists yet (typical for the first message of a new chat).

    Returns:
        UUID string, or None when not found and not creating.

    Side effects:
        GETs `/conversations`; may POST a new row when `create_if_missing`.
    """
    if not chat_lid:
        return None
    with _conv_cache_lock:
        cached = _conv_id_cache.get(chat_lid)
    if cached:
        return cached
    rows = _client().select(
        "conversations",
        {"chat_lid": f"eq.{chat_lid}", "select": "id", "limit": "1"},
    )
    if rows:
        cid = rows[0]["id"]
        with _conv_cache_lock:
            _conv_id_cache[chat_lid] = cid
        return cid
    if not create_if_missing:
        return None
    created = _client().upsert(
        "conversations",
        [{"chat_lid": chat_lid}],
        on_conflict="chat_lid",
    )
    cid = created[0]["id"] if created else None
    if cid:
        with _conv_cache_lock:
            _conv_id_cache[chat_lid] = cid
    return cid


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of `d` with keys whose value is None removed."""
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


def get_conversation(chat_lid: str) -> Optional[Dict[str, Any]]:
    """Fetch the conversation row for `chat_lid`.

    Inputs:
        chat_lid: WhatsApp chat identifier.

    Returns:
        The row dict, or None if no conversation is registered yet.

    Side effects:
        One GET against `conversations`.
    """
    if not chat_lid:
        return None
    rows = _client().select(
        "conversations",
        {"chat_lid": f"eq.{chat_lid}", "limit": "1"},
    )
    return rows[0] if rows else None


def upsert_conversation(
    chat_lid: str,
    phone: Optional[str] = None,
    name: Optional[str] = None,
    ai_blocked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Create or update a conversation row.

    Inputs:
        chat_lid: WhatsApp chat identifier (required).
        phone: Phone number (digits, no +).
        name: Display name. NOTE: there is no `name` column on
            `conversations` in the current schema. To persist a name use
            `upsert_lead_profile(chat_lid, {"nome": name})`. This parameter is
            accepted here for API symmetry and is intentionally ignored.
        ai_blocked: When set, also derives `status` ('blocked' vs 'active').

    Returns:
        The upserted row.

    Side effects:
        Upserts on conflict `chat_lid`. Updates the conv-id cache.

    Raises:
        ValueError if chat_lid is empty.
        requests.HTTPError on Supabase failure.
    """
    if not chat_lid:
        raise ValueError("chat_lid is required")
    row: Dict[str, Any] = {"chat_lid": chat_lid}
    if phone is not None:
        row["phone"] = phone
    if ai_blocked is not None:
        row["ai_blocked"] = bool(ai_blocked)
        row["status"] = "blocked" if ai_blocked else "active"
    result = _client().upsert("conversations", [row], on_conflict="chat_lid")
    out = result[0] if result else {}
    if out.get("id"):
        with _conv_cache_lock:
            _conv_id_cache[chat_lid] = out["id"]
    # Forward `name` to lead_profiles silently if requested; documented above.
    if name:
        try:
            upsert_lead_profile(chat_lid, {"nome": name})
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def append_message(chat_lid: str, message: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a single message, idempotent on (conversation, message_id).

    Inputs:
        chat_lid: WhatsApp chat identifier.
        message: Either a Z-API event dict `{timestamp, data}` or a
            normalized dict (see `_normalize_message`).

    Returns:
        The upserted message row, or {} if the underlying upsert returned
        nothing.

    Side effects:
        Resolves (or creates) the conversation; upserts on
        (conversation_id, message_id). Messages missing an upstream
        `messageId` are stored under a deterministic `local:<sha1>` id so
        replay does not duplicate.

    Raises:
        ValueError on empty `chat_lid`.
        requests.HTTPError on Supabase failure.
    """
    if not chat_lid:
        raise ValueError("chat_lid is required")
    conv_id = _resolve_conversation_id(chat_lid, create_if_missing=True)
    if not conv_id:
        raise RuntimeError(f"Could not resolve or create conversation for {chat_lid}")
    row = _normalize_message(chat_lid, message)
    row["conversation_id"] = conv_id
    result = _client().upsert(
        "messages",
        [row],
        on_conflict="conversation_id,message_id",
    )
    return result[0] if result else {}


def load_history(chat_lid: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return messages for `chat_lid` in chronological order.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        limit: Optional max number of rows. None returns all.

    Returns:
        List of message rows ordered by `event_ts` ascending. Empty list
        when the conversation is unknown.

    Side effects:
        One GET against `messages`.
    """
    conv_id = _resolve_conversation_id(chat_lid)
    if not conv_id:
        return []
    params: Dict[str, Any] = {
        "conversation_id": f"eq.{conv_id}",
        "order": "event_ts.asc",
    }
    if limit is not None and int(limit) > 0:
        params["limit"] = str(int(limit))
    return _client().select("messages", params)


# ---------------------------------------------------------------------------
# Lead profiles
# ---------------------------------------------------------------------------


def get_lead_profile(chat_lid: str) -> Optional[Dict[str, Any]]:
    """Fetch the lead profile row joined to `chat_lid`.

    Inputs:
        chat_lid: WhatsApp chat identifier.

    Returns:
        The profile row, or None.

    Side effects:
        One GET against `lead_profiles`.
    """
    conv_id = _resolve_conversation_id(chat_lid)
    if not conv_id:
        return None
    rows = _client().select(
        "lead_profiles",
        {"conversation_id": f"eq.{conv_id}", "limit": "1"},
    )
    return rows[0] if rows else None


def upsert_lead_profile(chat_lid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Merge-upsert the lead profile fields for `chat_lid`.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        data: Free-form dict. Known keys are mapped to their columns;
            unknown keys are preserved in the `raw` jsonb column.

    Returns:
        The upserted profile row.

    Side effects:
        Resolves (or creates) the conversation; upserts on `conversation_id`.

    Raises:
        requests.HTTPError on Supabase failure.
    """
    conv_id = _resolve_conversation_id(chat_lid, create_if_missing=True)
    if not conv_id:
        raise RuntimeError(f"Could not resolve conversation for {chat_lid}")

    payload: Dict[str, Any] = {"conversation_id": conv_id}
    raw_extras: Dict[str, Any] = {}
    for k, v in (data or {}).items():
        if k in LEAD_PROFILE_COLUMNS:
            payload[k] = _parse_iso(v) if k == "atualizado_em" else v
        elif k == "raw" and isinstance(v, dict):
            raw_extras.update(v)
        else:
            raw_extras[k] = v
    if "tentativas_retomada" in payload and payload["tentativas_retomada"] is not None:
        try:
            payload["tentativas_retomada"] = int(payload["tentativas_retomada"])
        except (TypeError, ValueError):
            payload["tentativas_retomada"] = 0
    payload["raw"] = raw_extras
    result = _client().upsert(
        "lead_profiles",
        [payload],
        on_conflict="conversation_id",
    )
    return result[0] if result else {}


def set_ai_blocked(chat_lid: str, blocked: bool) -> Dict[str, Any]:
    """Flip the AI-block flag for a chat.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        blocked: True to block the bot, False to re-enable.

    Returns:
        The upserted conversation row.

    Side effects:
        Upsert on `conversations`; also sets `status` accordingly.
    """
    return upsert_conversation(chat_lid, ai_blocked=bool(blocked))


# ---------------------------------------------------------------------------
# Followups
# ---------------------------------------------------------------------------


def schedule_followup(chat_lid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a follow-up job in status='scheduled'.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        payload: Dict containing at least `tipo` (one of '1h','24h','15d')
            and a target timestamp under either `target_at` or `horario_iso`.
            Optional `phone`.

    Returns:
        The upserted job row (includes `id`). Empty dict if the input was
        invalid and no row was written.

    Side effects:
        Resolves (or creates) the conversation; upserts on
        (conversation_id, tipo, target_at).

    Raises:
        ValueError when `tipo` is missing/invalid or the target timestamp
        cannot be parsed.
    """
    if not chat_lid:
        raise ValueError("chat_lid is required")
    tipo = (payload or {}).get("tipo")
    if tipo not in VALID_FOLLOWUP_TIPOS:
        raise ValueError(f"invalid followup tipo={tipo!r}; expected one of {VALID_FOLLOWUP_TIPOS}")
    target_at = _parse_iso(payload.get("target_at") or payload.get("horario_iso"))
    if not target_at:
        raise ValueError("missing or unparseable target_at / horario_iso")

    conv_id = _resolve_conversation_id(chat_lid, create_if_missing=True)
    row = {
        "conversation_id": conv_id,
        "tipo": tipo,
        "target_at": target_at,
        "status": "scheduled",
        "phone": payload.get("phone"),
        "payload": payload,
    }
    result = _client().upsert(
        "followup_jobs",
        [row],
        on_conflict="conversation_id,tipo,target_at",
    )
    return result[0] if result else {}


def mark_followup_sent(job_id: str) -> Dict[str, Any]:
    """Mark a scheduled follow-up job as sent (status='sent', sent_at=now).

    Inputs:
        job_id: UUID of the `followup_jobs` row.

    Returns:
        The updated row, or {} when no scheduled row matched.

    Side effects:
        PATCH on `followup_jobs`.

    Raises:
        ValueError when `job_id` is empty.
    """
    if not job_id:
        raise ValueError("job_id is required")
    result = _client().patch(
        "followup_jobs",
        {"id": f"eq.{job_id}", "status": "eq.scheduled"},
        {"status": "sent", "sent_at": _now_iso()},
    )
    return result[0] if result else {}


def mark_followup_failed(job_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Mark a scheduled follow-up job as failed.

    Inputs:
        job_id: UUID of the `followup_jobs` row.
        reason: Optional short reason stored inside `payload.failure_reason`.

    Returns:
        The updated row, or {} when no row matched.

    Side effects:
        PATCH on `followup_jobs`. Does not send any WhatsApp message.

    Raises:
        ValueError when `job_id` is empty.
        requests.HTTPError on Supabase failure.
    """
    if not job_id:
        raise ValueError("job_id is required")
    patch: Dict[str, Any] = {"status": "failed"}
    if reason:
        rows = _client().select(
            "followup_jobs",
            {"id": f"eq.{job_id}", "select": "payload", "limit": "1"},
        )
        payload = dict((rows[0] or {}).get("payload") or {}) if rows else {}
        payload["failure_reason"] = reason
        payload["failed_at"] = _now_iso()
        patch["payload"] = payload
    result = _client().patch(
        "followup_jobs",
        {"id": f"eq.{job_id}", "status": "eq.scheduled"},
        patch,
    )
    return result[0] if result else {}


def mark_followup_sent_by_tipo(chat_lid: str, tipo: str) -> List[Dict[str, Any]]:
    """Mark every scheduled follow-up of `tipo` for `chat_lid` as sent.

    Inputs:
        chat_lid: WhatsApp chat identifier.
        tipo: One of `VALID_FOLLOWUP_TIPOS`.

    Returns:
        List of updated rows (typically 0 or 1). Empty when the conversation
        is unknown or no scheduled row matches.

    Side effects:
        PATCH on `followup_jobs`.

    Notes:
        Used by the runtime when a follow-up timer fires — the runtime knows
        the chat and the tipo but not the row id.
    """
    if not chat_lid or tipo not in VALID_FOLLOWUP_TIPOS:
        return []
    conv_id = _resolve_conversation_id(chat_lid)
    if not conv_id:
        return []
    return _client().patch(
        "followup_jobs",
        {
            "conversation_id": f"eq.{conv_id}",
            "tipo": f"eq.{tipo}",
            "status": "eq.scheduled",
        },
        {"status": "sent", "sent_at": _now_iso()},
    )


def cancel_followup(
    job_id: Optional[str] = None,
    chat_lid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Cancel one or all pending follow-ups.

    Inputs:
        job_id: When provided, cancel that specific row.
        chat_lid: When provided (and `job_id` is None), cancel every
            scheduled follow-up of that chat.

    Returns:
        List of updated rows.

    Side effects:
        PATCH on `followup_jobs`.

    Raises:
        ValueError when neither argument is provided.
    """
    if not job_id and not chat_lid:
        raise ValueError("provide job_id or chat_lid")
    if job_id:
        return _client().patch(
            "followup_jobs",
            {"id": f"eq.{job_id}"},
            {"status": "cancelled"},
        )
    conv_id = _resolve_conversation_id(chat_lid or "")
    if not conv_id:
        return []
    return _client().patch(
        "followup_jobs",
        {"conversation_id": f"eq.{conv_id}", "status": "eq.scheduled"},
        {"status": "cancelled"},
    )


def list_pending_followups(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """List follow-ups in status='scheduled'.

    Inputs:
        now: Optional UTC datetime. When provided, only rows whose
            `target_at <= now` are returned (i.e. "due now or earlier").
            When None, every scheduled row is returned regardless of time —
            this is what the runtime startup uses to re-arm timers.

    Returns:
        List of job rows ordered by `target_at` ascending. Each row carries
        an embedded `conversations` object with the corresponding `chat_lid`
        so callers don't need a second query to resolve it.

    Side effects:
        One GET against `followup_jobs`.
    """
    params: Dict[str, Any] = {
        "select": "*,conversations(chat_lid)",
        "status": "eq.scheduled",
        "order": "target_at.asc",
    }
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        params["target_at"] = f"lte.{now.astimezone(timezone.utc).isoformat()}"
    return _client().select("followup_jobs", params)


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


def upsert_meeting(chat_lid: str, meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update a meeting row keyed by (conversation, scheduled_for).

    Inputs:
        chat_lid: WhatsApp chat identifier.
        meeting: Dict with at least a target timestamp under either
            `scheduled_for` or `datetime`. Optional: `agendado_em`,
            `meet_link`, `event_id`, `phone`, `status` (defaults to
            'scheduled').

    Returns:
        The upserted meeting row.

    Side effects:
        Resolves (or creates) the conversation; upserts on
        (conversation_id, scheduled_for).

    Raises:
        ValueError when no parseable scheduled timestamp is present, or when
        `status` is not in the allowed set.
    """
    if not chat_lid:
        raise ValueError("chat_lid is required")
    scheduled_for = _parse_iso(meeting.get("scheduled_for") or meeting.get("datetime"))
    if not scheduled_for:
        raise ValueError("missing or unparseable scheduled_for / datetime")
    status = meeting.get("status") or "scheduled"
    if status not in VALID_MEETING_STATUSES:
        raise ValueError(f"invalid meeting status={status!r}")

    conv_id = _resolve_conversation_id(chat_lid, create_if_missing=True)
    row = _drop_none({
        "conversation_id": conv_id,
        "phone": meeting.get("phone"),
        "scheduled_for": scheduled_for,
        "agendado_em": _parse_iso(meeting.get("agendado_em")),
        "meet_link": meeting.get("meet_link"),
        "event_id": meeting.get("event_id"),
        "status": status,
        "raw": meeting,
    })
    result = _client().upsert(
        "meetings",
        [row],
        on_conflict="conversation_id,scheduled_for",
    )
    return result[0] if result else {}


def update_meeting_status(
    meeting_id: Optional[str] = None,
    chat_lid: Optional[str] = None,
    datetime: Optional[str] = None,  # noqa: A002 (shadowing intentional, matches spec)
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Update a meeting's status, located by id or by (chat_lid, datetime).

    Inputs:
        meeting_id: UUID of the meeting row.
        chat_lid: WhatsApp chat identifier (used with `datetime`).
        datetime: ISO timestamp matching the meeting's `scheduled_for`.
        status: New status, must be one of `VALID_MEETING_STATUSES`.

    Returns:
        List of updated rows (usually 1, possibly 0 when nothing matches).

    Side effects:
        PATCH on `meetings`.

    Raises:
        ValueError when locator args are insufficient or status is invalid.
    """
    if not status or status not in VALID_MEETING_STATUSES:
        raise ValueError(f"invalid or missing status={status!r}")
    patch = {"status": status}
    if meeting_id:
        return _client().patch("meetings", {"id": f"eq.{meeting_id}"}, patch)
    if chat_lid and datetime:
        conv_id = _resolve_conversation_id(chat_lid)
        scheduled_for = _parse_iso(datetime)
        if not conv_id or not scheduled_for:
            return []
        return _client().patch(
            "meetings",
            {"conversation_id": f"eq.{conv_id}", "scheduled_for": f"eq.{scheduled_for}"},
            patch,
        )
    raise ValueError("provide meeting_id or (chat_lid and datetime)")


def list_active_meetings(chat_lid: Optional[str] = None) -> List[Dict[str, Any]]:
    """List meetings in status='scheduled', optionally filtered by chat.

    Inputs:
        chat_lid: When provided, only meetings of that conversation.

    Returns:
        List of meeting rows ordered by `scheduled_for` ascending. Each row
        carries an embedded `conversations.chat_lid` so callers can rebuild
        the legacy reunioes.json shape without an extra lookup.

    Side effects:
        One GET against `meetings`.
    """
    params: Dict[str, Any] = {
        "select": "*,conversations(chat_lid)",
        "status": "eq.scheduled",
        "order": "scheduled_for.asc",
    }
    if chat_lid:
        conv_id = _resolve_conversation_id(chat_lid)
        if not conv_id:
            return []
        params["conversation_id"] = f"eq.{conv_id}"
    return _client().select("meetings", params)


# ---------------------------------------------------------------------------
# Handoffs
# ---------------------------------------------------------------------------


def has_successful_handoff(chat_lid: str, new_phone: str) -> bool:
    """Return True when there is already a successful handoff for (chat, phone).

    Used as the dedup gate before dispatching a new handoff via Z-API: if a row
    with `success=true` already exists for this conversation+phone, the system
    should NOT redispatch. Rows with `success=false` are previous failed
    attempts and do not block a retry.

    Inputs:
        chat_lid: WhatsApp chat identifier of the lead who indicated the handoff.
        new_phone: Normalized E.164 phone (no '+') of the contact being handed off.

    Returns:
        True if at least one handoff row exists with success=true; False otherwise
        (including when the conversation doesn't exist yet).

    Side effects:
        One GET against `handoffs`.
    """
    if not chat_lid or not new_phone:
        return False
    conv_id = _resolve_conversation_id(chat_lid)
    if not conv_id:
        return False
    rows = _client().select(
        "handoffs",
        {
            "conversation_id": f"eq.{conv_id}",
            "new_phone": f"eq.{new_phone}",
            "success": "eq.true",
            "select": "id",
            "limit": "1",
        },
    )
    return bool(rows)


def record_handoff_attempt(
    chat_lid: str,
    new_phone: str,
    new_contact_name: Optional[str] = None,
    new_contact_role: Optional[str] = None,
    descricao: Optional[str] = None,
) -> Optional[str]:
    """Insert a new handoff row in state success=false (attempt in progress).

    Called BEFORE the Z-API dispatch. The returned id is later passed to
    `mark_handoff_success` or `mark_handoff_failure` once the dispatch resolves.

    Inputs:
        chat_lid: Conversation identifier.
        new_phone: Normalized phone of the contact being handed off.
        new_contact_name / new_contact_role / descricao: Optional metadata.

    Returns:
        UUID of the inserted row, or None on failure / missing conversation.

    Side effects:
        POST on `handoffs`; resolves/creates the conversation.
    """
    if not chat_lid or not new_phone:
        return None
    conv_id = _resolve_conversation_id(chat_lid, create_if_missing=True)
    if not conv_id:
        return None
    row = _drop_none(
        {
            "conversation_id": conv_id,
            "new_phone": new_phone,
            "new_contact_name": new_contact_name,
            "new_contact_role": new_contact_role,
            "descricao": descricao,
            "success": False,
        }
    )
    # Plain INSERT (cada tentativa é uma linha nova; id é gerado server-side).
    # Não usa upsert porque não há chave natural — re-disparo proposital cria
    # nova linha pra preservar o histórico de tentativas.
    cli = _client()
    r = cli._request(
        "POST",
        "/handoffs",
        headers={"Prefer": "return=representation"},
        data=json.dumps([row]),
    )
    try:
        inserted = r.json() or []
    except ValueError:
        inserted = []
    if not inserted:
        return None
    return (inserted[0] or {}).get("id")


def mark_handoff_success(handoff_id: str) -> Dict[str, Any]:
    """Flip a handoff row to success=true with confirmed_at=now.

    Called from the async dispatch thread once Z-API returns ok.

    Inputs:
        handoff_id: UUID returned by `record_handoff_attempt`.

    Returns:
        The updated row, or {} when no row matched.

    Side effects:
        PATCH on `handoffs`.
    """
    if not handoff_id:
        return {}
    result = _client().patch(
        "handoffs",
        {"id": f"eq.{handoff_id}"},
        {"success": True, "confirmed_at": _now_iso()},
    )
    return result[0] if result else {}


def mark_handoff_failure(handoff_id: str, error_detail: Optional[str] = None) -> Dict[str, Any]:
    """Record the failure reason on a handoff row (leaves success=false).

    The row stays with success=false so the dedup gate in `has_successful_handoff`
    keeps allowing retries on the next turn.

    Inputs:
        handoff_id: UUID returned by `record_handoff_attempt`.
        error_detail: Short message to persist for debugging.

    Returns:
        The updated row, or {} when no row matched.

    Side effects:
        PATCH on `handoffs`.
    """
    if not handoff_id:
        return {}
    patch: Dict[str, Any] = {"success": False}
    if error_detail:
        patch["error_detail"] = error_detail[:500]
    result = _client().patch(
        "handoffs",
        {"id": f"eq.{handoff_id}"},
        patch,
    )
    return result[0] if result else {}


# ---------------------------------------------------------------------------
# Risks / assumptions
# ---------------------------------------------------------------------------
#
# - `conversations` is assumed to have columns: id (uuid), chat_lid (text,
#   unique), phone (text), status (text, check active|blocked|closed),
#   ai_blocked (bool). There is NO `name` column; `upsert_conversation(name=...)`
#   forwards to lead_profiles.nome.
#
# - `lead_profiles` is assumed to expose the columns listed in
#   LEAD_PROFILE_COLUMNS plus `raw` (jsonb) and a `conversation_id` UNIQUE.
#
# - `messages` is assumed to have a UNIQUE on (conversation_id, message_id).
#   message_id is nullable in the DB but NEVER stored as NULL by this module —
#   the fallback `local:<sha1>` guarantees idempotency under re-runs.
#
# - `followup_jobs.tipo` is constrained to ('1h','24h','15d') and
#   `followup_jobs.status` to ('scheduled','sent','cancelled','failed').
#   Sending an out-of-set value will be rejected by Postgres.
#
# - `meetings.status` is constrained to ('scheduled','cancelled','completed').
#
# - All timestamps are stored as `timestamptz`. Naive ISO strings produced by
#   `datetime.now().isoformat()` on the bot host are interpreted as
#   America/Sao_Paulo before conversion to UTC.
#
# - The in-process `_conv_id_cache` assumes single-process semantics (Flask
#   dev server, gunicorn with --preload, etc.). Under multi-process WSGI the
#   cache is per-worker and that's fine because resolution is read-only and
#   always reflects the DB.
