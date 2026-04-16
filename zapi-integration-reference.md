# Z-API Integration Reference

Referência plug-and-play da integração com a Z-API para bots de WhatsApp.

---

## 1. Configuração Base

```python
API_TOKEN   = "E1014DD5B87C05F2DF8F2547"          # api token da instância
INSTANCE_ID = "3EFABF270A13919DE95AD6F4B026E35E"   # instance id
SEC_TOKEN   = os.environ.get("ZAPI_SEC_TOKEN")      # client-token (variável de ambiente)

BASE_URL = f"https://api.z-api.io/instances/{INSTANCE_ID}/token/{API_TOKEN}"
HEADERS  = {"client-token": SEC_TOKEN}
```

**Variável de ambiente obrigatória (`.env`):**
```env
ZAPI_SEC_TOKEN=seu_client_token_aqui
```

> O `client-token` fica no painel da Z-API em **Security Token**. Diferente do `api_token`, ele fica no header — não na URL.

---

## 2. Endpoints de Envio

### Enviar texto

```
POST {BASE_URL}/send-text
```

```json
{
  "phone": "5511999999999",
  "message": "Olá, tudo bem?",
  "delayTyping": 3,
  "messageId": "<id-da-msg-a-citar>"
}
```

- `delayTyping` (opcional): segundos de "digitando..." antes de enviar. Mín 1, máx 15.
- `messageId` (opcional): ID de uma mensagem recebida para citar (reply/marcar).

**Função pronta:**

```python
def send_text(phone: str, message: str, delay_typing: int = 0, reply_id: str = None):
    payload = {"phone": phone, "message": message}
    if delay_typing > 0:
        payload["delayTyping"] = min(delay_typing, 15)
    if reply_id:
        payload["messageId"] = reply_id
    return requests.post(f"{BASE_URL}/send-text", json=payload, headers=HEADERS)
```

**Cálculo de delay proporcional ao texto:**
```python
def calcular_delay(texto: str, wpm: int = 80) -> int:
    delay = len(texto) * 60 / (wpm * 5)
    return max(1, min(int(delay), 15))
```

---

## 3. Webhooks recebidos

Configure no painel Z-API → Webhooks, apontando para sua URL pública.

### 3.1 `/webhook/receive` — Mensagem recebida

**Campos do payload:**

| Campo                | Tipo    | Descrição                                               |
|----------------------|---------|---------------------------------------------------------|
| `phone`              | string  | Número do remetente (`"5511999999999"`)                 |
| `chatLid`            | string  | ID único do chat (`"5511999999999@lid"`) — pode vir vazio |
| `fromMe`             | bool    | `true` = enviada pelo bot; `false` = enviada pelo lead  |
| `messageId`          | string  | ID único da mensagem                                    |
| `referenceMessageId` | string  | ID da mensagem citada (reply)                          |
| `momment`            | number  | Timestamp Unix da mensagem                              |
| `text`               | object  | `{ "message": "texto" }`                               |
| `audio`              | object  | `{ "audioUrl": "...", "mimeType": "..." }`             |
| `image`              | object  | `{ "imageUrl": "...", "caption": "..." }`              |
| `video`              | object  | `{ "videoUrl": "...", "caption": "..." }`              |
| `document`           | object  | `{ "documentUrl": "...", "mimeType": "...", "caption": "..." }` |

> **Atenção:** `chatLid` pode vir vazio em alguns cenários. Sempre use o fallback:
> ```python
> chatLid = data.get("chatLid") or f"{phone}@lid"
> ```

**Exemplos de payload:**

```json
// Texto
{
  "phone": "5511999999999",
  "chatLid": "5511999999999@lid",
  "fromMe": false,
  "messageId": "ABC123",
  "referenceMessageId": "",
  "momment": 1712600000,
  "text": { "message": "Oi, quero saber mais" }
}

// Áudio
{
  "phone": "5511999999999",
  "chatLid": "5511999999999@lid",
  "fromMe": false,
  "messageId": "ABC124",
  "audio": {
    "audioUrl": "https://cdn.z-api.io/.../audio.ogg",
    "mimeType": "audio/ogg; codecs=opus"
  }
}

// Imagem
{
  "phone": "5511999999999",
  "chatLid": "5511999999999@lid",
  "fromMe": false,
  "messageId": "ABC125",
  "image": {
    "imageUrl": "https://cdn.z-api.io/.../image.jpg",
    "caption": "Olha essa foto"
  }
}
```

---

### 3.2 `/webhook/presence` — Presença do usuário

**Campos do payload:**

| Campo     | Tipo   | Descrição               |
|-----------|--------|-------------------------|
| `phone`   | string | Número do lead          |
| `chatLid` | string | ID do chat (pode vir vazio) |
| `name`    | string | Nome do contato         |
| `status`  | string | Status atual            |

**Status possíveis:**

| Status        | Significado                               |
|---------------|-------------------------------------------|
| `COMPOSING`   | Lead está digitando                       |
| `RECORDING`   | Lead está gravando áudio                  |
| `PAUSED`      | Lead parou de digitar/gravar              |
| `AVAILABLE`   | Lead está online                          |
| `UNAVAILABLE` | Lead está offline                         |

---

### 3.3 Demais webhooks (responder só 200)

| Rota                      | Quando dispara                        |
|---------------------------|---------------------------------------|
| `/webhook/connect`        | Instância conectou                    |
| `/webhook/send`           | Bot enviou uma mensagem               |
| `/webhook/message-status` | Mensagem entregue/lida                |

---

## 4. Implementação completa funcional

Inclui: debounce de resposta, controle de presença (não responde enquanto digita), reinício de resposta no PAUSED/AVAILABLE/UNAVAILABLE, e fallback de chatLid.

```python
import os
import threading
import requests
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# --- Configuração Z-API ---
API_TOKEN   = "E1014DD5B87C05F2DF8F2547"
INSTANCE_ID = "3EFABF270A13919DE95AD6F4B026E35E"
SEC_TOKEN   = os.environ.get("ZAPI_SEC_TOKEN")
BASE_URL    = f"https://api.z-api.io/instances/{INSTANCE_ID}/token/{API_TOKEN}"
HEADERS     = {"client-token": SEC_TOKEN}

# --- Estado em memória ---
pending_timers: dict[str, threading.Timer] = {}   # chatLid → timer aguardando envio
answer_list:    dict[str, dict] = {}              # chatLid → último payload recebido
composing_set:  set = set()                       # chatLids que estão digitando/gravando
_timer_lock = threading.Lock()


# ------------------------------------------------------------------
# Envio
# ------------------------------------------------------------------

def send_text(phone: str, message: str, delay_typing: int = 0, reply_id: str = None):
    payload = {"phone": phone, "message": message}
    if delay_typing > 0:
        payload["delayTyping"] = min(delay_typing, 15)
    if reply_id:
        payload["messageId"] = reply_id
    return requests.post(f"{BASE_URL}/send-text", json=payload, headers=HEADERS)


def calcular_delay(texto: str, wpm: int = 80) -> int:
    delay = len(texto) * 60 / (wpm * 5)
    return max(1, min(int(delay), 15))


# ------------------------------------------------------------------
# Lógica de resposta com debounce
# ------------------------------------------------------------------

def _disparar_resposta(phone: str, chatLid: str):
    """Chamado pelo timer após o debounce. Só executa se o lead não estiver digitando."""
    with _timer_lock:
        pending_timers.pop(chatLid, None)

    if chatLid in composing_set:
        # Lead ainda está digitando, não responde agora
        return

    data = answer_list.get(chatLid)
    if not data:
        return

    texto_entrada = extrair_texto(data)
    if not texto_entrada:
        return

    # -------------------------------------------------------
    # IMPLEMENTE SUA LÓGICA DE IA AQUI
    # -------------------------------------------------------
    resposta = processar_mensagem(phone, chatLid, texto_entrada, data)
    # -------------------------------------------------------

    if resposta:
        partes = [p.strip() for p in resposta.split("\n\n") if p.strip()]
        for i, parte in enumerate(partes):
            delay = calcular_delay(parte, wpm=400 if i == 0 else 250)
            send_text(phone, parte, delay_typing=delay)


def agendar_resposta(phone: str, chatLid: str, data: dict):
    """Salva o payload e agenda um timer de debounce antes de responder."""
    answer_list[chatLid] = data

    texto = extrair_texto(data)
    # Delay proporcional ao texto: 3s base + 0.04s por char, máx 12s
    delay = min(3.0 + len(texto) * 0.04, 12.0)

    with _timer_lock:
        if chatLid in pending_timers:
            pending_timers[chatLid].cancel()

        t = threading.Timer(delay, _disparar_resposta, args=(phone, chatLid))
        pending_timers[chatLid] = t
        t.start()


# ------------------------------------------------------------------
# Extração de conteúdo por tipo de mensagem
# ------------------------------------------------------------------

def extrair_texto(data: dict) -> str:
    """Extrai o conteúdo textual de qualquer tipo de mensagem Z-API."""

    text = data.get("text", {})
    if isinstance(text, dict) and text.get("message"):
        return text["message"]

    audio = data.get("audio", {})
    if isinstance(audio, dict) and audio.get("audioUrl"):
        # Para transcrever: baixe o audioUrl e passe para Whisper (openai.audio.transcriptions)
        return f"[AUDIO: {audio['audioUrl']}]"

    image = data.get("image", {})
    if isinstance(image, dict) and image.get("imageUrl"):
        caption = image.get("caption", "")
        return f"[IMAGEM{': ' + caption if caption else ''}: {image['imageUrl']}]"

    video = data.get("video", {})
    if isinstance(video, dict) and video.get("videoUrl"):
        caption = video.get("caption", "")
        return f"[VIDEO{': ' + caption if caption else ''}: {video['videoUrl']}]"

    doc = data.get("document", {})
    if isinstance(doc, dict) and doc.get("documentUrl"):
        caption = doc.get("caption", "")
        return f"[DOCUMENTO{': ' + caption if caption else ''}: {doc['documentUrl']}]"

    return ""


# ------------------------------------------------------------------
# Sua lógica de negócio
# ------------------------------------------------------------------

def processar_mensagem(phone: str, chatLid: str, texto: str, raw_data: dict) -> str:
    """Substitua esta função pela sua IA / lógica de resposta."""
    return f"Você disse: {texto}"


# ------------------------------------------------------------------
# Webhooks
# ------------------------------------------------------------------

@app.route("/webhook/connect", methods=["POST"])
def webhook_connect():
    return {"status": "ok"}, 200


@app.route("/webhook/send", methods=["POST"])
def webhook_send():
    return {"status": "ok"}, 200


@app.route("/webhook/message-status", methods=["POST"])
def webhook_message_status():
    return {"status": "ok"}, 200


@app.route("/webhook/receive", methods=["POST"])
def webhook_receive():
    data    = request.json
    phone   = data.get("phone")
    from_me = data.get("fromMe")
    # Fallback: chatLid pode vir vazio
    chatLid = data.get("chatLid") or (f"{phone}@lid" if phone else None)

    if not chatLid:
        return {"status": "ignored", "reason": "chatLid ausente"}, 200

    if from_me:
        return {"status": "ignored", "reason": "mensagem própria"}, 200

    composing_set.discard(chatLid)
    agendar_resposta(phone, chatLid, data)

    return {"status": "ok"}, 200


@app.route("/webhook/presence", methods=["POST"])
def webhook_presence():
    data    = request.json
    phone   = data.get("phone")
    chatLid = data.get("chatLid") or (f"{phone}@lid" if phone else None)
    status  = data.get("status")

    if not chatLid:
        return {"status": "ok"}, 200

    if status in ("COMPOSING", "RECORDING"):
        # Lead está digitando: marca e cancela o timer de resposta
        composing_set.add(chatLid)
        with _timer_lock:
            if chatLid in pending_timers:
                pending_timers[chatLid].cancel()
                del pending_timers[chatLid]

    elif status in ("PAUSED", "AVAILABLE", "UNAVAILABLE"):
        # Lead parou de digitar / ficou online / ficou offline
        was_composing = chatLid in composing_set
        composing_set.discard(chatLid)

        # Se havia uma mensagem pendente e o timer foi cancelado, reinicia
        if was_composing and chatLid not in pending_timers and chatLid in answer_list:
            saved_data = answer_list[chatLid]
            agendar_resposta(phone, chatLid, saved_data)

    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(port=5001)
```

---

## 5. Notas importantes

- **`chatLid`** é a chave primária do chat. Sempre use-o, nunca só o `phone`.
- **`fromMe: true`** = mensagem enviada pelo próprio bot. Ignore sempre para não criar loop.
- **Debounce:** O bot não responde imediatamente. Espera alguns segundos para acumular múltiplas mensagens antes de processar — isso evita responder mensagem por mensagem quando o lead manda várias seguidas.
- **Presença:** O timer é cancelado enquanto o lead digita (`COMPOSING`/`RECORDING`) e reiniciado quando ele para (`PAUSED`/`AVAILABLE`/`UNAVAILABLE`).
- **Dividir resposta em partes:** Separe por `\n\n` e envie como múltiplos balões — mais natural no WhatsApp.
- **`delayTyping`** simula o bot "digitando" antes de enviar. Use proporcional ao tamanho da mensagem.
