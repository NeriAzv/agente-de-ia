import re
from datetime import datetime

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat


def normalizar_telefone(numero: str, default_region: str = "BR") -> str | None:
    """Normaliza qualquer formato comum para dígitos puros em E.164 sem '+'.

    Aceita: '+55 11 97953-7732', '(11) 97953-7732', '0055 11 97953-7732',
    '+33 1 40 73 73 73', '+1 609-452-1000', etc.

    Returns:
        String só com dígitos pronta para Z-API (ex.: '5573999846024'),
        ou None se o número não puder ser parseado ou não for válido.
    """
    if not numero:
        return None
    bruto = numero.strip()
    if bruto.startswith("00"):
        bruto = "+" + bruto[2:]
    try:
        parsed = phonenumbers.parse(bruto, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    return e164.lstrip("+")


HORAS_EXTENSO = {
    "meia noite": "00:00", "uma": "01:00", "duas": "02:00", "três": "03:00",
    "quatro": "04:00", "cinco": "05:00", "seis": "06:00", "sete": "07:00",
    "oito": "08:00", "nove": "09:00", "dez": "10:00", "onze": "11:00",
    "meio dia": "12:00", "doze": "12:00", "treze": "13:00", "quatorze": "14:00",
    "catorze": "14:00", "quinze": "15:00", "dezesseis": "16:00", "dezessete": "17:00",
    "dezoito": "18:00", "dezenove": "19:00", "vinte": "20:00", "vinte e uma": "21:00",
    "vinte e duas": "22:00", "vinte e três": "23:00"
}


def normalizar_data(data_str: str):
    """
    Converte data para objeto date.
    O LLM já resolve "amanhã", "terça", etc. para YYYY-MM-DD.
    Aceita também DD/MM/YYYY e DD/MM (ano atual).
    """
    data_str = data_str.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(data_str, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.strptime(data_str, "%d/%m").replace(year=datetime.now().year).date()
    except ValueError:
        pass
    return None


def normalizar_horario(horario_str: str) -> str | None:
    """
    Converte qualquer formato de horário para HH:MM.
    Aceita: "14h", "14:00", "14:35", "duas da tarde", "duas", etc.
    """
    horario_str = horario_str.lower().strip()

    # Formato numérico: "14:35" ou "14h35" ou "14h"
    match = re.search(r'(\d{1,2})[h:](\d{2})', horario_str)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    match = re.search(r'(\d{1,2})h', horario_str)
    if match:
        return f"{int(match.group(1)):02d}:00"

    # Formato por extenso: "duas da tarde", "duas", etc.
    for extenso, hora in HORAS_EXTENSO.items():
        if extenso in horario_str:
            hora_num = int(hora.split(":")[0])
            # Ajusta PM: "da tarde" ou "da noite"
            if ("tarde" in horario_str or "noite" in horario_str) and hora_num < 12:
                hora_num += 12
            return f"{hora_num:02d}:00"

    return None
