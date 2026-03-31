# Agente de IA — SDR Autônomo para WhatsApp

Agente inteligente de vendas que atua como SDR (Sales Development Representative) via WhatsApp. Ele segmenta leads, conduz conversas de qualificação e toma decisões autônomas — como agendar reuniões, marcar retornos e criar eventos com Google Meet — baseado na interpretação da própria IA.

## Como funciona

```
WhatsApp User
     │
     ▼
  Z-API (webhook)
     │  POST /webhook/receive
     ▼
Flask (db_app.py — porta 5001)
     │  salva histórico por chat
     ▼
Agent_AI (AI_agent.py)
     │
     ├── LangGraph + GPT-4o-mini  →  gera resposta
     ├── Detecta intenção de reunião  →  cria evento Google Meet
     └── Envia resposta via Z-API (delay de 7s)
```

O Flask recebe os webhooks do Z-API (WhatsApp). Cada mensagem recebida é salva em um arquivo JSON por conversa e passada ao agente. O agente processa o histórico completo, gera uma resposta e, se necessário, age autonomamente (cria reunião, agenda retorno, etc.).

## Fluxo de qualificação

1. **Saudação** — coleta nome e segmento da empresa
2. **Descoberta** — entende os desafios e sistemas atuais
3. **Qualificação** — tamanho do time e volume de operações
4. **Segmentação** — perguntas específicas por setor (ex: hotel → nº de quartos)
5. **Pitch** — apresenta o BTime direcionado à dor identificada
6. **Demo** — convida para demonstração
7. **Agendamento** — capta data/hora e cria evento com Google Meet automaticamente

## Stack

| Camada | Tecnologia |
|--------|-----------|
| Exposição local | ngrok |
| API HTTP | Flask |
| WhatsApp | Z-API |
| IA / Agente | LangGraph + LangChain + OpenAI GPT-4o-mini |
| Calendário | Google Calendar API (OAuth2) |

## Estrutura de arquivos

```
agente-de-ia/
├── app/
│   ├── AI_agent.py          # Classe Agent_AI — toda a lógica do agente
│   ├── db_app.py            # Flask + endpoints de webhook
│   └── client_secret.json   # Credenciais OAuth2 Google Calendar
├── {chatLid}.json           # Histórico de conversa por chat (gerado em runtime)
├── reunioes.json            # Reuniões agendadas (gerado em runtime)
└── token.json               # Token OAuth Google (gerado no primeiro login)
```

## Variáveis de ambiente

Crie um arquivo `.env` ou exporte no terminal antes de rodar:

```env
OPENAI_API_KEY=sk-...
ZAPI_SEC_TOKEN=...
```

## Como rodar

### 1. Instalar dependências

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Se não houver `requirements.txt`, as dependências principais são:
> `flask`, `openai`, `langchain`, `langchain-openai`, `langgraph`, `google-auth-oauthlib`, `google-api-python-client`, `requests`

### 2. Configurar Google Calendar

Na primeira execução, uma janela de autenticação OAuth2 abrirá no navegador. Autorize o acesso ao Google Calendar. O token será salvo em `token.json` para usos futuros.

### 3. Subir o servidor Flask

```bash
cd app
python db_app.py
```

O servidor sobe na porta **5001**.

### 4. Expor via ngrok

```bash
ngrok http 5001
```

Copie a URL gerada (ex: `https://xxxx.ngrok.io`) e configure-a como webhook no painel do Z-API.

### 5. Configurar webhooks no Z-API

No painel Z-API, configure os seguintes endpoints apontando para a URL do ngrok:

| Evento | Endpoint |
|--------|----------|
| Mensagem recebida | `POST /webhook/receive` |
| Presença (digitando) | `POST /webhook/presence` |
| Status de mensagem | `POST /webhook/message-status` |
| Conexão | `POST /webhook/connect` |

## Funcionalidades do agente

- **Agendamento autônomo**: detecta intenção de reunião na conversa e cria evento no Google Calendar com link do Google Meet automaticamente
- **Lembrete automático**: envia mensagem de lembrete 30 minutos antes da reunião
- **Consciência de presença**: cancela respostas pendentes se o usuário começar a digitar
- **Normalização de data/hora**: entende formatos como `"amanhã"`, `"terça-feira"`, `"duas da tarde"`, `"14h"`, etc.
- **Histórico por conversa**: mantém contexto completo de cada chat em arquivo JSON separado
- **Whitelist de chats**: responde apenas a IDs autorizados (configurado em `AI_agent.py`)
- **Delay de resposta**: aguarda 7 segundos antes de responder, simulando comportamento humano
