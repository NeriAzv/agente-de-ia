import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_SP = ZoneInfo("America/Sao_Paulo")

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from colors import GREEN, RED, RESET


# Diretório raiz de app/ (um nível acima deste arquivo)
_APP_DIR = os.path.dirname(os.path.dirname(__file__))

GOOGLE_SCOPES      = ["https://www.googleapis.com/auth/calendar"]
ORGANIZER_EMAIL    = "marcela.bezerra@btime.com.br"
MASTER_EMAIL       = "guilherme.neri@btime.com.br"   # conta que cria todos os eventos
GOOGLE_TOKEN_FILE  = os.path.join(_APP_DIR, "token.json")
GOOGLE_SECRET_FILE = os.path.join(_APP_DIR, "client_secret.json")

# Mapeamento de produto → RD responsável (agenda consultada + convidada na reunião)
PRODUCT_ORGANIZERS: dict[str, str] = {
    "Squad AI":   "marcela.bezerra@btime.com.br",
    "SaaS Btime": "beatriz.santos@btime.com.br",
    "Ambos":      "marcela.bezerra@btime.com.br",
}


def get_organizer_for_product(produto_indicado: str | None) -> str:
    """Retorna o e-mail da RD responsável pelo produto indicado."""
    if not produto_indicado:
        return ORGANIZER_EMAIL
    return PRODUCT_ORGANIZERS.get(produto_indicado, ORGANIZER_EMAIL)


def get_calendar_service():
    """Retorna o serviço autenticado do Google Calendar."""
    creds = None

    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_SECRET_FILE, GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(GOOGLE_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def criar_evento_google_meet(
    data_reuniao,
    horario_reuniao: str,
    email_lead: str | None = None,
    nome_lead: str | None = None,
    duracao_min: int = 30,
    rd_email: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Cria evento no Google Calendar com link Google Meet.
    O evento é criado na conta mestre (MASTER_EMAIL).
    Participantes: MASTER_EMAIL (host) + lead + RD (rd_email).
    Retorna (meet_link, event_id) ou (None, None) em caso de erro.
    """
    try:
        service = get_calendar_service()

        inicio = datetime.combine(data_reuniao, datetime.strptime(horario_reuniao, "%H:%M").time())
        fim    = inicio + timedelta(minutes=duracao_min)

        titulo = f"Demo Btime - {nome_lead}" if nome_lead else "Demo Btime"

        attendees = [{"email": MASTER_EMAIL, "organizer": True, "responseStatus": "accepted"}]
        if rd_email:
            attendees.append({"email": rd_email, "responseStatus": "needsAction"})
        if email_lead:
            attendees.append({"email": email_lead, "displayName": nome_lead or ""})

        evento = {
            "summary": titulo,
            "organizer": {"email": MASTER_EMAIL},
            "start":   {"dateTime": inicio.isoformat(), "timeZone": "America/Sao_Paulo"},
            "end":     {"dateTime": fim.isoformat(),    "timeZone": "America/Sao_Paulo"},
            "attendees": attendees,
            "conferenceData": {
                "createRequest": {
                    "requestId": f"btime-{int(inicio.timestamp())}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"}
                }
            }
        }

        resultado = service.events().insert(
            calendarId=MASTER_EMAIL,
            body=evento,
            conferenceDataVersion=1
        ).execute()

        link     = resultado.get("hangoutLink") or resultado.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri")
        event_id = resultado.get("id")
        print(f"{GREEN}Evento criado: {resultado.get('htmlLink')} (id={event_id}){RESET}")
        return link, event_id

    except Exception as e:
        print(f"{RED}Erro ao criar evento Google Meet: {e}{RESET}")
        return None, None


def buscar_horarios_livres(
    data_reuniao,
    duracao_min: int = 30,
    inicio_comercial: int = 9,
    fim_comercial: int = 19,
    calendar_id: str = "primary",
) -> list[str]:
    """
    Retorna lista de horários livres (HH:MM) no dia informado,
    dentro do horário comercial (9-12 e 13-19) e sem conflito com eventos existentes.
    """
    try:
        service = get_calendar_service()

        dia_inicio = datetime.combine(data_reuniao, datetime.strptime(f"{inicio_comercial:02d}:00", "%H:%M").time())
        dia_fim    = datetime.combine(data_reuniao, datetime.strptime(f"{fim_comercial:02d}:00",    "%H:%M").time())

        resultado = service.events().list(
            calendarId=calendar_id,
            timeMin=dia_inicio.isoformat() + "-03:00",
            timeMax=dia_fim.isoformat()    + "-03:00",
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        ocupados = []
        for ev in resultado.get("items", []):
            start_dt = ev.get("start", {}).get("dateTime")
            end_dt   = ev.get("end",   {}).get("dateTime")

            if start_dt and end_dt:
                # Evento com hora definida
                s = datetime.fromisoformat(start_dt)
                e = datetime.fromisoformat(end_dt)
                if s.tzinfo:
                    s = s.astimezone(_SP).replace(tzinfo=None)
                if e.tzinfo:
                    e = e.astimezone(_SP).replace(tzinfo=None)
                ocupados.append((s, e))
            elif ev.get("start", {}).get("date"):
                # Evento de dia inteiro — só bloqueia se for opaco (ocupado de verdade)
                # Eventos informativos/transparentes (feriados, aniversários, etc.) são ignorados
                if ev.get("transparency", "opaque") == "opaque":
                    s = datetime.combine(data_reuniao, datetime.strptime(f"{inicio_comercial:02d}:00", "%H:%M").time())
                    e = datetime.combine(data_reuniao, datetime.strptime(f"{fim_comercial:02d}:00",    "%H:%M").time())
                    ocupados.append((s, e))

        livres = []
        slot = dia_inicio
        while slot + timedelta(minutes=duracao_min) <= dia_fim:
            slot_fim = slot + timedelta(minutes=duracao_min)
            hora = slot.hour + slot.minute / 60
            # Pula horário de almoço (12h-13h)
            em_horario_util = (9 <= hora < 12) or (13 <= hora < 19)
            if em_horario_util:
                conflito = any(s < slot_fim and slot < e for s, e in ocupados)
                if not conflito:
                    livres.append(slot.strftime("%H:%M"))
            slot += timedelta(minutes=duracao_min)

        print(f"{GREEN}Horários livres em {data_reuniao} [{calendar_id}]: {livres}{RESET}")
        return livres

    except Exception as e:
        print(f"{RED}Erro ao buscar horários livres [{calendar_id}]: {e}{RESET}")
        return []


def verificar_conflito_google_calendar(
    data_reuniao,
    horario_reuniao: str,
    duracao_min: int = 30,
    calendar_id: str = "primary",
) -> bool:
    """
    Verifica se já existe algum evento no Google Calendar no horário solicitado.
    Retorna True se houver conflito, False se o horário estiver livre.
    """
    try:
        service = get_calendar_service()

        inicio = datetime.combine(data_reuniao, datetime.strptime(horario_reuniao, "%H:%M").time())
        fim    = inicio + timedelta(minutes=duracao_min)

        resultado = service.events().list(
            calendarId=calendar_id,
            timeMin=inicio.isoformat() + "-03:00",
            timeMax=fim.isoformat() + "-03:00",
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        # Ignora eventos transparentes (informativos) — só conta os que bloqueiam o tempo
        eventos = [e for e in resultado.get("items", []) if e.get("transparency", "opaque") == "opaque"]
        if eventos:
            print(f"{RED}Conflito de horário [{calendar_id}]: {len(eventos)} evento(s) entre {inicio} e {fim}{RESET}")
            return True

        print(f"{GREEN}Horário livre [{calendar_id}]: nenhum evento entre {inicio} e {fim}{RESET}")
        return False

    except Exception as e:
        print(f"{RED}Erro ao verificar conflito [{calendar_id}]: {e}{RESET}")
        return False


def deletar_evento_google_calendar(event_id: str, calendar_id: str = MASTER_EMAIL) -> bool:
    """
    Remove um evento do Google Calendar pelo ID.
    Retorna True se deletado com sucesso.
    """
    try:
        service = get_calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        print(f"{GREEN}Evento {event_id} removido do Google Calendar.{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Erro ao deletar evento {event_id}: {e}{RESET}")
        return False
