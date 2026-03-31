import os
import json
import time
import threading
import tempfile
import subprocess
import base64
import requests
from dotenv import load_dotenv
load_dotenv(override=True)
from datetime import datetime, timedelta
from typing import List, Any, Dict

from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from colors import GREEN, RED, YELLOW, BLUE, RESET
from agent.calendar import criar_evento_google_meet, deletar_evento_google_calendar, verificar_conflito_google_calendar, buscar_horarios_livres
from agent.normalizers import normalizar_data, normalizar_horario
from agent.context import get_contexto


class Agent_AI:

    def __init__(self):
        self.api_token = "E1014DD5B87C05F2DF8F2547"
        self.zapi_sec_token = os.environ.get("ZAPI_SEC_TOKEN")
        self.instance_id = "3EFABF270A13919DE95AD6F4B026E35E"
        self.base_url = f"https://api.z-api.io/instances/{self.instance_id}/token/{self.api_token}"

        self.answer_list = {}
        self.pending_timers: dict[str, threading.Timer] = {}
        self.pending_texts: dict[str, str] = {}
        self.followup_timers: dict[str, list[threading.Timer]] = {}
        self.composing_set: set = set()

        self._restaurar_followups()

    # ------------------------------------------------------------------
    # Follow-up automático por inatividade
    # ------------------------------------------------------------------

    def _followup_callback(self, phone: str, chatLid: str, tipo: str):
        """Envia mensagem de follow-up (1h ou 24h) e limpa do arquivo."""
        try:
            headers = {"client-token": self.zapi_sec_token}
            llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))

            # Carrega nome do lead se disponível
            nome = ""
            lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
            if os.path.exists(lead_info_path):
                with open(lead_info_path, "r", encoding="utf-8") as f:
                    lead_info = json.load(f)
                nome = lead_info.get("nome") or ""

            saudacao = f" {nome}" if nome else ""

            if tipo == "1h":
                instrucao = (
                    f"O lead{saudacao} não respondeu há 1 hora. "
                    "Escreva uma mensagem curta e natural tentando retomar a conversa, "
                    "demonstrando interesse genuíno em ajudar. "
                    "Sem emojis. Sem JSON. Só o texto da mensagem."
                )
            else:  # 24h
                instrucao = (
                    f"O lead{saudacao} não respondeu há 24 horas. "
                    "Escreva uma mensagem muito curta avisando que você está disponível "
                    "caso ele queira continuar a conversa, sem pressionar. "
                    "Sem emojis. Sem JSON. Só o texto da mensagem."
                )

            file = os.path.join("chats", chatLid, "history.json")
            historico = []
            if os.path.exists(file):
                with open(file, "r", encoding="utf-8") as f:
                    historico = json.load(f)
            mensagens_recentes = self.extrair_mensagens(historico, chatLid)[-6:]

            resp = llm.invoke([
                SystemMessage(content=get_contexto()),
                *[
                    HumanMessage(content=m["content"]) if m["role"] == "user"
                    else AIMessage(content=m["content"])
                    for m in mensagens_recentes
                ],
                SystemMessage(content=instrucao),
            ])

            url = f"{self.base_url}/send-text"
            requests.post(url, json={"phone": phone, "message": resp.content.strip()}, headers=headers)
            print(f"{GREEN}Follow-up {tipo} enviado para {chatLid}{RESET}")
        except Exception as e:
            print(f"{RED}Erro no follow-up {tipo} para {chatLid}: {e}{RESET}")
        finally:
            self._limpar_followup_arquivo(chatLid, tipo)

    def agendar_followups(self, phone: str, chatLid: str):
        """Agenda timers de 1h e 24h e salva os horários no lead_info.json."""
        # Checa se o agente indicou que follow-up é necessário
        lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
        if os.path.exists(lead_info_path):
            try:
                with open(lead_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                necessita = info.get("necessita_followup", True)
                motivo    = info.get("motivo_followup", "")
                if necessita is False:
                    print(f"{YELLOW}Follow-up desativado para {chatLid}: {motivo}{RESET}")
                    self.cancelar_followups(chatLid)
                    return
            except Exception:
                pass

        self.cancelar_followups(chatLid)

        agora = datetime.now()
        alvo_1h  = agora + timedelta(hours=1)
        alvo_24h = agora + timedelta(hours=24)

        t1h = threading.Timer(3600.0, self._followup_callback, args=(phone, chatLid, "1h"))
        t24h = threading.Timer(86400.0, self._followup_callback, args=(phone, chatLid, "24h"))

        self.followup_timers[chatLid] = [t1h, t24h]
        t1h.start()
        t24h.start()

        # Persiste horários no lead_info.json
        lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
        info = {}
        if os.path.exists(lead_info_path):
            with open(lead_info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        info["followups_agendados"] = [
            {"tipo": "1h",  "horario_iso": alvo_1h.isoformat(),  "phone": phone},
            {"tipo": "24h", "horario_iso": alvo_24h.isoformat(), "phone": phone},
        ]
        os.makedirs(os.path.join("chats", chatLid), exist_ok=True)
        with open(lead_info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)

        print(f"{GREEN}Follow-ups agendados para {chatLid}: 1h às {alvo_1h.strftime('%d/%m %H:%M')} | 24h às {alvo_24h.strftime('%d/%m %H:%M')}{RESET}")

    def cancelar_followups(self, chatLid: str):
        """Cancela timers de follow-up (quando lead responde) e remove do arquivo."""
        timers = self.followup_timers.pop(chatLid, [])
        for t in timers:
            t.cancel()
        if timers:
            print(f"{YELLOW}Follow-ups cancelados para {chatLid} (lead respondeu){RESET}")
        self._limpar_followup_arquivo(chatLid, None)

    def _limpar_followup_arquivo(self, chatLid: str, tipo):
        """Remove follow-up do arquivo — se tipo=None remove todos."""
        lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
        if not os.path.exists(lead_info_path):
            return
        try:
            with open(lead_info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            agendados = info.get("followups_agendados", [])
            if tipo is None:
                info["followups_agendados"] = []
            else:
                info["followups_agendados"] = [x for x in agendados if x.get("tipo") != tipo]
            with open(lead_info_path, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"{RED}Erro ao limpar followup_arquivo para {chatLid}: {e}{RESET}")

    # ------------------------------------------------------------------
    # Início de conversa outbound (lead sem histórico)
    # ------------------------------------------------------------------

    def iniciar_conversa(self, phone: str, chatLid: str) -> bool:
        """
        Verifica se o lead não tem histórico ainda e, se for o caso,
        gera e envia a mensagem de abertura (lead frio).
        Retorna True se a mensagem foi enviada, False caso contrário.
        """

        lead_dir = os.path.join("chats", chatLid)
        file     = os.path.join(lead_dir, "history.json")

        # Verifica se já existe mensagem do lead (fromMe=false) — mensagens só nossas não contam
        tem_mensagem_do_lead = False
        if os.path.exists(file):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    historico = json.load(f)
                tem_mensagem_do_lead = any(
                    not item.get("data", {}).get("fromMe", True)
                    for item in historico
                )
            except Exception:
                pass

        if tem_mensagem_do_lead:
            print(f"{YELLOW}[iniciar_conversa] {chatLid} já tem mensagem do lead, nenhuma abertura enviada{RESET}")
            return False

        try:
            headers = {"client-token": self.zapi_sec_token}
            llm     = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))

            # Tenta carregar dados do lead já conhecidos (se houver lead_info sem histórico)
            contexto_lead = ""
            lead_info_path = os.path.join(lead_dir, "lead_info.json")
            if os.path.exists(lead_info_path):
                with open(lead_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                campos_uteis = {k: v for k, v in info.items() if v and k in ("nome", "empresa", "segmento_mercado")}
                if campos_uteis:
                    contexto_lead = "Dados conhecidos do lead: " + ", ".join(f"{k}: {v}" for k, v in campos_uteis.items()) + "."

            instrucao = (
                "Você está iniciando o contato com um lead frio pelo WhatsApp. "
                + (contexto_lead + " " if contexto_lead else "")
                + "Escreva a primeira mensagem seguindo as diretrizes de abertura de conversa: "
                "se tiver contexto do lead, abra com um gancho relevante sobre uma dor do segmento dele; "
                "se não tiver contexto, apresente-se brevemente e faça uma pergunta aberta sobre o negócio. "
                "Nunca cumprimente com 'tudo bem?' ou similar. Nunca faça pitch genérico longo. "
                "Nunca peça demo na primeira mensagem. Sem emojis. Sem JSON. Só o texto."
            )

            resposta = llm.invoke([
                SystemMessage(content=get_contexto()),
                SystemMessage(content=instrucao),
            ])

            mensagem = resposta.content.strip()

            # Envia a mensagem
            url = f"{self.base_url}/send-text"
            requests.post(url, json={"phone": phone, "message": mensagem}, headers=headers)
            print(f"{GREEN}[iniciar_conversa] Mensagem de abertura enviada para {chatLid}{RESET}")

            # Agenda follow-ups de inatividade
            self.agendar_followups(phone, chatLid)

            return True

        except Exception as e:
            print(f"{RED}[iniciar_conversa] Erro ao enviar mensagem de abertura para {chatLid}: {e}{RESET}")
            return False

    def _restaurar_followups(self):
        """Na inicialização, restaura timers de follow-up pendentes salvos em arquivo."""
        chats_dir = "chats"
        if not os.path.exists(chats_dir):
            return
        agora = datetime.now()
        for chatLid in os.listdir(chats_dir):
            lead_info_path = os.path.join(chats_dir, chatLid, "lead_info.json")
            if not os.path.exists(lead_info_path):
                continue
            try:
                with open(lead_info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                if info.get("necessita_followup") is False:
                    print(f"{YELLOW}Follow-up ignorado na restauração para {chatLid} (necessita_followup=false){RESET}")
                    continue
                agendados = info.get("followups_agendados", [])
                if not agendados:
                    continue
                timers = []
                for item in agendados:
                    tipo  = item.get("tipo")
                    phone = item.get("phone")
                    alvo  = datetime.fromisoformat(item["horario_iso"])
                    segundos = (alvo - agora).total_seconds()
                    if segundos <= 0:
                        # Já passou — dispara em 5s para não bloquear o start
                        segundos = 5.0
                        print(f"{YELLOW}Follow-up {tipo} para {chatLid} já passou, disparando em 5s{RESET}")
                    else:
                        print(f"{GREEN}Follow-up {tipo} restaurado para {chatLid}: dispara em {int(segundos)}s ({alvo.strftime('%d/%m %H:%M')}){RESET}")
                    t = threading.Timer(segundos, self._followup_callback, args=(phone, chatLid, tipo))
                    timers.append(t)
                    t.start()
                if timers:
                    self.followup_timers[chatLid] = timers
            except Exception as e:
                print(f"{RED}Erro ao restaurar follow-up para {chatLid}: {e}{RESET}")

    # ------------------------------------------------------------------
    # Envio de mensagens
    # ------------------------------------------------------------------

    def send_message(self, phone, chatLid, data) -> dict:

        def __send_message(chatLid, data):
            print(f" {GREEN} > __send_message chamado para {chatLid} {RESET}")
            if chatLid in self.answer_list:
                print(f" {YELLOW} > Respondendo para {chatLid} {RESET}")
                if True:
                    print(f" {BLUE} > Enviando resposta para {chatLid} {RESET}")
                    phone = data.get("phone")
                    url = f"{self.base_url}/send-text"
                    print(f"{GREEN}Url para envio: {url}{RESET}")
                    payload = {
                        "phone": phone,
                        "message": self.get_ai_response(
                            self.extrair_mensagens([{"data": data, "timestamp": data.get("momment", 0)}])
                        ),
                    }
                    headers = {"client-token": self.zapi_sec_token}
                    response = requests.request("POST", url, data=payload, headers=headers)
                else:
                    print(f" {RED}\n\n > Chat {chatLid} não permitido {RESET}")
            else:
                print(f" {RED} > Nenhuma resposta encontrada para {chatLid} {RESET}")

        self.answer_list[chatLid] = data
        text = data.get("text", {})
        texto = text.get("message", "") if isinstance(text, dict) else str(text or "")
        delay = min(3.0 + len(texto) * 0.04, 12.0)
        threading.Timer(delay, __send_message, args=(chatLid, data)).start()

    # ------------------------------------------------------------------
    # Transcrição de áudio
    # ------------------------------------------------------------------

    def _transcrever_audio(self, audio_url: str, mime_type: str = "") -> str:
        """Baixa o áudio e transcreve via OpenAI Whisper."""
        try:
            from openai import OpenAI
            response = requests.get(audio_url, timeout=30)
            response.raise_for_status()

            # Usa o mimeType do payload Z-API (mais confiável que Content-Type HTTP)
            tipo = mime_type or response.headers.get("Content-Type", "")
            suffix = ".ogg"
            if "mp4" in tipo or "m4a" in tipo:
                suffix = ".mp4"
            elif "mpeg" in tipo or "mp3" in tipo:
                suffix = ".mp3"
            elif "wav" in tipo:
                suffix = ".wav"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name

            try:
                client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
                with open(tmp_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="pt",
                    )
                print(f"{GREEN}[transcrever_audio] Transcrição: {transcript.text}{RESET}")
                return transcript.text.strip()
            finally:
                os.unlink(tmp_path)

        except Exception as e:
            print(f"{RED}[transcrever_audio] Erro: {e}{RESET}")
            return ""

    # ------------------------------------------------------------------
    # Análise de imagem
    # ------------------------------------------------------------------

    def _analisar_imagem(self, image_url: str, caption: str = "") -> str:
        """Baixa a imagem e descreve o conteúdo via GPT-4o Vision."""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

            prompt = (
                "Descreva o conteúdo desta imagem de forma objetiva e concisa, "
                "como se estivesse relatando para um assistente de vendas. "
                "Foque em informações relevantes como textos visíveis, produtos, documentos, "
                "capturas de tela ou qualquer dado útil para o contexto de uma conversa comercial."
            )
            if caption:
                prompt += f" A legenda enviada junto com a imagem foi: \"{caption}\"."

            messages_vision = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ]

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages_vision,
                max_tokens=500,
            )
            descricao = response.choices[0].message.content.strip()
            print(f"{GREEN}[analisar_imagem] Descrição: {descricao}{RESET}")
            return descricao

        except Exception as e:
            print(f"{RED}[analisar_imagem] Erro: {e}{RESET}")
            return caption or ""

    # ------------------------------------------------------------------
    # Análise de vídeo
    # ------------------------------------------------------------------

    def _analisar_video(self, video_url: str, caption: str = "") -> str:
        """Baixa o vídeo, extrai frames + áudio via ffmpeg e descreve via GPT-4o Vision + Whisper."""
        tmp_video = None
        tmp_dir = None
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

            # Baixa o vídeo
            response = requests.get(video_url, timeout=60)
            response.raise_for_status()

            tipo = response.headers.get("Content-Type", "")
            suffix = ".mp4"
            if "webm" in tipo:
                suffix = ".webm"
            elif "avi" in tipo:
                suffix = ".avi"
            elif "mov" in tipo or "quicktime" in tipo:
                suffix = ".mov"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(response.content)
                tmp_video = tmp.name

            tmp_dir = tempfile.mkdtemp()

            # Verifica a duração do vídeo via ffprobe
            LIMITE_FRAMES_SEGUNDOS = 150  # 2min30s
            duracao_result = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                tmp_video,
            ], capture_output=True, text=True, timeout=30)
            try:
                duracao = float(duracao_result.stdout.strip())
            except (ValueError, TypeError):
                duracao = 0

            # Extrai 1 frame por segundo apenas se o vídeo tiver até 2min30s
            extrair_frames = duracao <= LIMITE_FRAMES_SEGUNDOS
            if not extrair_frames:
                print(f"{YELLOW}[analisar_video] Vídeo com {duracao:.1f}s excede limite de {LIMITE_FRAMES_SEGUNDOS}s — frames não serão extraídos{RESET}")

            if extrair_frames:
                if duracao <= 30:
                    fps = 3
                elif duracao <= 60:
                    fps = 2
                else:
                    fps = 1
                print(f"{YELLOW}[analisar_video] Extraindo frames a {fps}fps (duração: {duracao:.1f}s){RESET}")
                try:
                    subprocess.run([
                        "ffmpeg", "-i", tmp_video,
                        "-vf", f"fps={fps}",
                        "-q:v", "2",
                        os.path.join(tmp_dir, "frame%04d.jpg"),
                        "-y", "-loglevel", "error",
                    ], check=True, timeout=120)
                except Exception as e:
                    print(f"{RED}[analisar_video] Erro ao extrair frames: {e}{RESET}")

            # Extrai o áudio do vídeo
            audio_path = os.path.join(tmp_dir, "audio.mp3")
            try:
                subprocess.run([
                    "ffmpeg", "-i", tmp_video,
                    "-vn", "-ar", "16000", "-ac", "1", "-q:a", "4",
                    audio_path,
                    "-y", "-loglevel", "error",
                ], check=True, timeout=120)
            except Exception as e:
                print(f"{YELLOW}[analisar_video] Erro ao extrair áudio: {e}{RESET}")

            # Coleta os frames extraídos (pode ser vazio se vídeo excedeu o limite)
            MAX_FRAMES = 20
            frames = sorted([
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".jpg")
            ])
            # Se extraiu mais frames do que o limite, faz amostragem uniforme
            if len(frames) > MAX_FRAMES:
                step = len(frames) / MAX_FRAMES
                frames = [frames[int(i * step)] for i in range(MAX_FRAMES)]
                print(f"{YELLOW}[analisar_video] Frames reduzidos para {MAX_FRAMES} por amostragem uniforme{RESET}")

            # Transcreve o áudio via Whisper (se existir)
            transcricao = ""
            if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                try:
                    with open(audio_path, "rb") as audio_file:
                        transcript = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file,
                            language="pt",
                        )
                    transcricao = transcript.text.strip()
                    print(f"{GREEN}[analisar_video] Transcrição do áudio: {transcricao}{RESET}")
                except Exception as e:
                    print(f"{YELLOW}[analisar_video] Sem áudio ou erro na transcrição: {e}{RESET}")

            # Se não há nem frames nem transcrição, retorna fallback
            if not frames and not transcricao:
                print(f"{RED}[analisar_video] Nenhum frame ou áudio disponível{RESET}")
                return caption or ""

            # Monta o payload multimodal
            content = []
            if frames:
                prompt = (
                    "Analise os frames deste vídeo e descreva o conteúdo de forma objetiva e concisa, "
                    "como se estivesse relatando para um assistente de vendas. "
                    "Foque em informações relevantes como textos visíveis, produtos, demonstrações, "
                    "documentos ou qualquer dado útil para o contexto de uma conversa comercial."
                )
            else:
                prompt = (
                    "O vídeo era longo demais para extrair frames, mas o áudio foi transcrito. "
                    "Com base na transcrição abaixo, descreva o conteúdo de forma objetiva e concisa "
                    "como se estivesse relatando para um assistente de vendas."
                )
            if caption:
                prompt += f" A legenda enviada junto com o vídeo foi: \"{caption}\"."
            if transcricao:
                prompt += f" O áudio do vídeo foi transcrito como: \"{transcricao}\"."
            content.append({"type": "text", "text": prompt})

            for frame_path in frames:
                with open(frame_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                })

            response_ai = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": content}],
                max_tokens=700,
            )
            descricao = response_ai.choices[0].message.content.strip()
            print(f"{GREEN}[analisar_video] Descrição: {descricao}{RESET}")
            return descricao

        except subprocess.CalledProcessError as e:
            print(f"{RED}[analisar_video] Erro no ffmpeg: {e}{RESET}")
            return caption or ""
        except Exception as e:
            print(f"{RED}[analisar_video] Erro: {e}{RESET}")
            return caption or ""
        finally:
            if tmp_video and os.path.exists(tmp_video):
                os.unlink(tmp_video)
            if tmp_dir and os.path.exists(tmp_dir):
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)

    def _extrair_documento(self, doc_url: str, mime_type: str = "", caption: str = "") -> str:
        """Baixa e extrai o texto de documentos (DOCX, TXT, etc.)."""
        try:
            response = requests.get(doc_url, timeout=30)
            response.raise_for_status()

            tipo = mime_type or response.headers.get("Content-Type", "")
            suffix = ".docx"
            if "spreadsheet" in tipo or "xlsx" in tipo or "excel" in tipo:
                suffix = ".xlsx"
            elif "csv" in tipo:
                suffix = ".csv"
            elif "plain" in tipo or "txt" in tipo:
                suffix = ".txt"
            elif "pdf" in tipo:
                suffix = ".pdf"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name

            try:
                conteudo = ""
                if suffix == ".docx":
                    from docx import Document
                    doc = Document(tmp_path)
                    paragrafos = [p.text for p in doc.paragraphs if p.text.strip()]
                    conteudo = "\n".join(paragrafos)
                elif suffix == ".xlsx":
                    import openpyxl
                    wb = openpyxl.load_workbook(tmp_path, data_only=True)
                    partes = []
                    for sheet in wb.worksheets:
                        linhas = []
                        for row in sheet.iter_rows(values_only=True):
                            celulas = [str(c) if c is not None else "" for c in row]
                            if any(c.strip() for c in celulas):
                                linhas.append("\t".join(celulas))
                        if linhas:
                            partes.append(f"[Aba: {sheet.title}]\n" + "\n".join(linhas))
                    conteudo = "\n\n".join(partes)
                elif suffix == ".csv":
                    import csv
                    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                        reader = csv.reader(f)
                        linhas = ["\t".join(row) for row in reader if any(c.strip() for c in row)]
                    conteudo = "\n".join(linhas)
                elif suffix == ".pdf":
                    import fitz  # pymupdf
                    doc_pdf = fitz.open(tmp_path)
                    partes_pdf = []
                    for i in range(len(doc_pdf)):
                        texto = doc_pdf[i].get_text().strip()
                        if texto:
                            partes_pdf.append(f"[Página {i + 1}]\n{texto}")
                    conteudo = "\n\n".join(partes_pdf)
                    doc_pdf.close()
                elif suffix == ".txt":
                    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                        conteudo = f.read()

                if conteudo:
                    prefixo = f'[Documento enviado{f": {caption}" if caption else ""}]\n'
                    return prefixo + conteudo.strip()
                return caption or ""
            finally:
                os.unlink(tmp_path)

        except Exception as e:
            print(f"{RED}[_extrair_documento] Erro: {e}{RESET}")
            return caption or ""

    # ------------------------------------------------------------------
    # Extração de mensagens
    # ------------------------------------------------------------------

    def extrair_mensagens(self, data: List[Dict[str, Any]], chatLid: str = "") -> List[Dict[str, str]]:
        """
        Extrai as mensagens do histórico no formato esperado pela OpenAI.
        Áudios são transcritos via Whisper e a transcrição é persistida no
        histórico para evitar chamadas repetidas à API.
        """
        mensagens = []

        if not isinstance(data, list):
            print(f"{RED}Erro: data não é uma lista{RESET}")
            return mensagens

        data_ordenada = sorted(data, key=lambda x: x.get("timestamp", 0))
        historico_modificado = False

        # Monta índice messageId → conteúdo para lookup de mensagens citadas
        def _extrair_conteudo_simples(msg_data: dict) -> str:
            text = msg_data.get("text", {})
            if isinstance(text, dict):
                content = text.get("message", "")
            else:
                content = str(text) if text else ""
            if not content:
                audio = msg_data.get("audio", {})
                if isinstance(audio, dict):
                    content = audio.get("transcricao", "")
            if not content:
                image = msg_data.get("image", {})
                if isinstance(image, dict):
                    content = image.get("descricao", "") or image.get("caption", "")
            if not content:
                video = msg_data.get("video", {})
                if isinstance(video, dict):
                    content = video.get("descricao", "") or video.get("caption", "")
            if not content:
                document = msg_data.get("document", {})
                if isinstance(document, dict):
                    content = document.get("conteudo", "") or document.get("caption", "") or ""
            return (content or "").strip()

        indice_mensagens: Dict[str, str] = {}
        for _item in data_ordenada:
            _msg_data = _item.get("data", {})
            _msg_id = _msg_data.get("messageId", "")
            if _msg_id:
                _conteudo = _extrair_conteudo_simples(_msg_data)
                if _conteudo:
                    indice_mensagens[_msg_id] = _conteudo

        for item in data_ordenada:
            mensagem_data = item.get("data", {})
            text_data = mensagem_data.get("text", {})

            if isinstance(text_data, dict):
                message_content = text_data.get("message", "")
            else:
                message_content = str(text_data) if text_data else ""

            # Se não há texto, verifica se é mensagem de áudio
            if not message_content:
                audio_data = mensagem_data.get("audio", {})
                if isinstance(audio_data, dict):
                    # Usa transcrição já salva para evitar chamar Whisper novamente
                    message_content = audio_data.get("transcricao", "")
                    if not message_content:
                        audio_url = audio_data.get("audioUrl", "")
                        mime_type = audio_data.get("mimeType", "")
                        if audio_url:
                            print(f"{YELLOW}[extrair_mensagens] Áudio detectado, transcrevendo...{RESET}")
                            message_content = self._transcrever_audio(audio_url, mime_type)
                            if message_content:
                                # Persiste transcrição no item para não retranscrever depois
                                audio_data["transcricao"] = message_content
                                historico_modificado = True

            # Se não há texto nem áudio, verifica se é mensagem de imagem
            if not message_content:
                image_data = mensagem_data.get("image", {})
                if isinstance(image_data, dict):
                    # Usa descrição já salva para evitar chamar a API novamente
                    descricao = image_data.get("descricao", "")
                    if not descricao:
                        image_url = image_data.get("imageUrl", "")
                        caption = image_data.get("caption", "")
                        if image_url:
                            print(f"{YELLOW}[extrair_mensagens] Imagem detectada, analisando...{RESET}")
                            descricao = self._analisar_imagem(image_url, caption)
                            if descricao:
                                image_data["descricao"] = descricao
                                historico_modificado = True
                    if descricao:
                        img_url = image_data.get("imageUrl", "")
                        caption = image_data.get("caption", "")
                        text_part = f"[O usuário enviou uma imagem.{' Legenda: ' + caption + '.' if caption else ''} Análise automática: {descricao}]"
                        if img_url:
                            message_content = [
                                {"type": "text", "text": text_part},
                                {"type": "image_url", "image_url": {"url": img_url, "detail": "low"}},
                            ]
                        else:
                            message_content = text_part

            # Se não há texto, áudio nem imagem, verifica se é mensagem de vídeo
            if not message_content:
                video_data = mensagem_data.get("video", {})
                if isinstance(video_data, dict):
                    descricao = video_data.get("descricao", "")
                    if not descricao:
                        video_url = video_data.get("videoUrl", "")
                        caption = video_data.get("caption", "")
                        if video_url:
                            print(f"{YELLOW}[extrair_mensagens] Vídeo detectado, analisando...{RESET}")
                            descricao = self._analisar_video(video_url, caption)
                            if descricao:
                                video_data["descricao"] = descricao
                                historico_modificado = True
                    if descricao:
                        message_content = f"[O usuário enviou um vídeo. Análise automática: {descricao}]"

            # Se não há texto, áudio, imagem nem vídeo, verifica se é documento
            if not message_content:
                doc_data = mensagem_data.get("document", {})
                if isinstance(doc_data, dict):
                    message_content = doc_data.get("conteudo", "")
                    if not message_content:
                        doc_url = doc_data.get("documentUrl", "")
                        mime_type = doc_data.get("mimeType", "")
                        caption = doc_data.get("caption", "")
                        if doc_url:
                            print(f"{YELLOW}[extrair_mensagens] Documento detectado, extraindo...{RESET}")
                            message_content = self._extrair_documento(doc_url, mime_type, caption)
                            if message_content:
                                doc_data["conteudo"] = message_content
                                historico_modificado = True

            if not message_content:
                continue

            from_me = mensagem_data.get("fromMe", False)
            role = "assistant" if from_me else "user"

            # Se a mensagem é uma resposta a outra, injeta o contexto da citação
            ref_id = mensagem_data.get("referenceMessageId", "")
            if ref_id and isinstance(message_content, str):
                if ref_id in indice_mensagens:
                    citado = indice_mensagens[ref_id]
                    max_citado = 200
                    if len(citado) > max_citado:
                        citado = citado[:max_citado] + "..."
                    message_content = f'[Em resposta a: "{citado}"]\n{message_content.strip()}'
                else:
                    message_content = f'[Em resposta a uma mensagem anterior]\n{message_content.strip()}'

            if isinstance(message_content, str):
                mensagens.append({"role": role, "content": message_content.strip()})
            else:
                mensagens.append({"role": role, "content": message_content})

        # Salva histórico atualizado se houve nova transcrição
        if historico_modificado and chatLid:
            try:
                file = os.path.join("chats", chatLid, "history.json")
                with open(file, "w", encoding="utf-8") as f:
                    json.dump(data_ordenada, f, indent=2, ensure_ascii=False)
                print(f"{GREEN}[extrair_mensagens] Transcrições salvas em {file}{RESET}")
            except Exception as e:
                print(f"{RED}[extrair_mensagens] Erro ao salvar transcrições: {e}{RESET}")

        return mensagens

    # ------------------------------------------------------------------
    # Geração de resposta com IA
    # ------------------------------------------------------------------

    def get_ai_response(self, phone, chatLid, data) -> str:

        # Lead respondeu — cancela follow-ups pendentes
        self.cancelar_followups(chatLid)

        self.answer_list[chatLid] = data

        # --- Nós do grafo ---

        _PALAVRAS_AGENDAMENTO = [
            "vou agendar", "vou marcar", "demo marcada", "reunião marcada",
            "reuniao marcada", "demo para o dia", "reunião para o dia",
            "reuniao para o dia", "agendei", "já vou confirmar",
        ]

        def processar(state):
            llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))
            msgs_llm = [
                SystemMessage(content=state["context"]),
                *[
                    HumanMessage(content=m["content"]) if m["role"] == "user"
                    else AIMessage(content=m["content"])
                    for m in state["messages"]
                ],
            ]
            resposta = llm.invoke(msgs_llm)
            content = resposta.content.strip()

            # Se parece confirmação de agendamento mas não é JSON, força re-invocação
            is_json = False
            try:
                parsed = json.loads(content.strip("```json").strip("```").strip())
                is_json = isinstance(parsed, dict) and "acao" in parsed
            except (json.JSONDecodeError, ValueError):
                pass

            if not is_json and any(p in content.lower() for p in _PALAVRAS_AGENDAMENTO):
                print(f"{YELLOW}[processar] Resposta parece agendamento mas não é JSON. Re-invocando para extrair JSON.{RESET}")
                resposta = llm.invoke(msgs_llm + [
                    AIMessage(content=content),
                    SystemMessage(content=(
                        "Sua resposta anterior indicou que vai agendar uma reunião, mas não estava no formato JSON exigido. "
                        "Responda APENAS com o JSON de agendamento, sem nenhum texto antes ou depois:\n"
                        '{"acao": "agendar_reuniao", "data": "YYYY-MM-DD", "horario": "HH:MM", "nome": "Nome Sobrenome", "email": "email@lead.com", "mensagem": "..."}'
                    )),
                ])
                print(f"{YELLOW}[processar] Resposta corrigida: {repr(resposta.content)}{RESET}")

            return {"messages": state["messages"] + [{"role": "assistant", "content": resposta.content}]}

        # --- Helpers de envio / agendamento ---

        def dividir_em_mensagens(texto: str) -> list[str]:
            """
            Divide a resposta em partes separadas por parágrafo (\n\n),
            como balões distintos no WhatsApp.
            """
            partes = [p.strip() for p in texto.split("\n\n") if p.strip()]
            return partes if partes else [texto]

        def enviar_mensagem(phone, texto, headers):
            """Envia uma mensagem de texto pro lead."""
            url = f"{self.base_url}/send-text"
            payload = {"phone": phone, "message": texto}
            response = requests.request("POST", url, data=payload, headers=headers)
            print(f"\n\n{GREEN}>Resposta enviada: {response.text}{RESET}")

        def gerar_e_enviar(instrucao: str, mensagens_contexto: list, headers):
            """Chama o LLM com uma instrução adicional e envia o resultado ao lead."""
            llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))
            resposta = llm.invoke([
                SystemMessage(content=get_contexto()),
                *[
                    HumanMessage(content=m["content"]) if m["role"] == "user"
                    else AIMessage(content=m["content"])
                    for m in mensagens_contexto
                ],
                SystemMessage(content=instrucao),
            ])
            for parte in dividir_em_mensagens(resposta.content):
                enviar_mensagem(phone, parte, headers)

        def verificar_consulta_disponibilidade(resultado, headers, mensagens_contexto) -> bool:
            """
            Verifica se o LLM quer consultar horários disponíveis para um dia.
            Busca os slots livres e envia as opções ao lead.
            """
            resposta = resultado["messages"][-1]["content"]
            try:
                limpa  = resposta.strip().strip("```json").strip("```").strip()
                parsed = json.loads(limpa)

                if parsed.get("acao") == "consultar_disponibilidade":
                    data_raw     = parsed.get("data", "")
                    data_reuniao = normalizar_data(data_raw)

                    if not data_reuniao:
                        print(f"{RED}[consultar_disponibilidade] Data inválida: {data_raw}{RESET}")
                        return False

                    alvo = datetime.combine(data_reuniao, datetime.min.time())

                    if alvo.date() <= datetime.now().date():
                        gerar_e_enviar(
                            f"O lead sugeriu {data_reuniao.strftime('%d/%m/%Y')} que já passou ou é hoje. "
                            "Peça de forma natural que escolha uma data futura.",
                            mensagens_contexto, headers
                        )
                        return True

                    if alvo.weekday() >= 5:
                        gerar_e_enviar(
                            f"O lead sugeriu {data_reuniao.strftime('%d/%m/%Y')} que cai em fim de semana. "
                            "Explique de forma natural que as demos acontecem apenas em dias úteis (segunda a sexta) e pergunte qual dia da semana funciona melhor pra ele.",
                            mensagens_contexto, headers
                        )
                        return True

                    livres = buscar_horarios_livres(data_reuniao)
                    data_fmt = data_reuniao.strftime("%d/%m")

                    if livres:
                        opcoes = ", ".join(livres[:3])
                        instrucao = (
                            f"Para o dia {data_fmt} temos disponibilidade às {opcoes}. "
                            "Apresente no máximo 3 opções de forma natural e curta, sem listar todos os horários. Pergunte qual prefere."
                        )
                    else:
                        instrucao = (
                            f"Não há horários disponíveis no dia {data_fmt}. "
                            "Informe o lead de forma natural e sugira que escolha outro dia."
                        )

                    gerar_e_enviar(instrucao, mensagens_contexto, headers)
                    return True

            except (json.JSONDecodeError, ValueError, KeyError):
                pass

            return False

        def verificar_reuniao(resultado, headers, mensagens_contexto) -> bool:
            """
            Verifica se a resposta é um agendamento de reunião/demo.
            Salva em reunioes.json e agenda lembrete 30min antes.
            """
            resposta = resultado["messages"][-1]["content"]
            print(f"{BLUE}[verificar_reuniao] Resposta bruta do LLM: {repr(resposta)}{RESET}")
            try:
                limpa = resposta.strip().strip("```json").strip("```").strip()
                print(f"{BLUE}[verificar_reuniao] JSON limpo para parse: {repr(limpa)}{RESET}")
                parsed = json.loads(limpa)
                print(f"{BLUE}[verificar_reuniao] JSON parsed: {parsed}{RESET}")

                if parsed.get("acao") == "agendar_reuniao":
                    data_raw    = parsed.get("data", "")
                    horario_raw = parsed.get("horario", "")
                    mensagem    = parsed.get("mensagem", "Reunião confirmada. Até lá.")
                    email_lead  = parsed.get("email") or None
                    nome_lead   = parsed.get("nome") or None

                    # Fallback: busca email/nome do lead_info.json se o LLM não informou
                    lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
                    lead_info = {}
                    if os.path.exists(lead_info_path):
                        with open(lead_info_path, "r", encoding="utf-8") as f:
                            lead_info = json.load(f)
                    email_lead = email_lead or lead_info.get("email") or None
                    nome_lead  = nome_lead  or lead_info.get("nome")  or None

                    # Guard: só agenda se o lead já foi qualificado (produto_indicado definido)
                    produto_indicado = lead_info.get("produto_indicado") or None
                    if not produto_indicado:
                        print(f"{YELLOW}[verificar_reuniao] Lead ainda não qualificado (produto_indicado=null). Bloqueando agendamento.{RESET}")
                        gerar_e_enviar(
                            "O lead tentou agendar uma reunião mas ainda não foi qualificado — você ainda não identificou se o produto indicado é Squad AI, SaaS Btime ou Ambos. "
                            "Não agende a reunião agora. Continue a conversa fazendo as perguntas de qualificação necessárias para entender o cenário do lead antes de propor a demo.",
                            mensagens_contexto, headers
                        )
                        return True

                    data_reuniao    = normalizar_data(data_raw)
                    horario_reuniao = normalizar_horario(horario_raw)

                    if not data_reuniao or not horario_reuniao:
                        print(f"{RED}Data/horário inválido: {data_raw} {horario_raw}{RESET}")
                        gerar_e_enviar(
                            "Não foi possível interpretar a data ou horário combinado. Peça ao lead, de forma natural, que informe novamente.",
                            mensagens_contexto, headers
                        )
                        return True

                    alvo  = datetime.combine(data_reuniao, datetime.strptime(horario_reuniao, "%H:%M").time())
                    agora = datetime.now()

                    if alvo <= agora:
                        print(f"{RED}Horário de reunião já passou: {alvo}{RESET}")
                        gerar_e_enviar(
                            f"O lead sugeriu uma data/horário que já passou ({alvo.strftime('%d/%m/%Y %H:%M')}). Peça a ele, de forma natural, que escolha uma data futura.",
                            mensagens_contexto, headers
                        )
                        return True

                    # Valida dia útil (seg–sex) e horário comercial (10h–17h)
                    if alvo.weekday() >= 5:  # 5=sábado, 6=domingo
                        print(f"{RED}Reunião em fim de semana: {alvo}{RESET}")
                        gerar_e_enviar(
                            f"O lead sugeriu {alvo.strftime('%d/%m/%Y')} que cai em um fim de semana. "
                            "Explique de forma natural que as demos acontecem apenas em dias úteis (segunda a sexta) "
                            "e pergunte qual dia da semana funciona melhor pra ele.",
                            mensagens_contexto, headers
                        )
                        return True

                    hora = alvo.hour + alvo.minute / 60
                    if hora < 10 or hora >= 17:
                        print(f"{RED}Horário fora do comercial: {alvo.strftime('%H:%M')}{RESET}")
                        gerar_e_enviar(
                            f"O lead sugeriu {alvo.strftime('%H:%M')} que está fora do horário disponível (10h às 17h). "
                            "Explique de forma natural e sugira um horário dentro da janela, entre 10h e 17h.",
                            mensagens_contexto, headers
                        )
                        return True

                    # Cancela reunião anterior do mesmo lead, se existir
                    arquivo_reunioes = "reunioes.json"
                    if os.path.exists(arquivo_reunioes):
                        with open(arquivo_reunioes, "r", encoding="utf-8") as f:
                            reunioes = json.load(f)
                    else:
                        reunioes = []

                    reunioes_filtradas = []
                    for r in reunioes:
                        if r.get("chatLid") == chatLid:
                            event_id_antigo = r.get("event_id")
                            if event_id_antigo:
                                print(f"{YELLOW}Removendo evento anterior: {event_id_antigo}{RESET}")
                                deletar_evento_google_calendar(event_id_antigo)
                            else:
                                print(f"{YELLOW}Reunião anterior sem event_id, não foi possível remover do Calendar.{RESET}")
                        else:
                            reunioes_filtradas.append(r)

                    if verificar_conflito_google_calendar(data_reuniao, horario_reuniao):
                        livres_no_dia = buscar_horarios_livres(data_reuniao)
                        if livres_no_dia:
                            opcoes = ", ".join(livres_no_dia[:3])
                            instrucao_conflito = (
                                f"O horário solicitado ({alvo.strftime('%d/%m/%Y às %H:%M')}) já está ocupado. "
                                f"Informe o lead de forma natural e sugira até 3 opções nesse dia: {opcoes}."
                            )
                        else:
                            instrucao_conflito = (
                                f"O horário solicitado ({alvo.strftime('%d/%m/%Y às %H:%M')}) está ocupado e não há mais horários livres nesse dia. "
                                "Informe o lead de forma natural e peça que escolha outro dia."
                            )
                        gerar_e_enviar(instrucao_conflito, mensagens_contexto, headers)
                        return True

                    meet_link, event_id = criar_evento_google_meet(data_reuniao, horario_reuniao, email_lead=email_lead, nome_lead=nome_lead)

                    reunioes_filtradas.append({
                        "phone": phone,
                        "chatLid": chatLid,
                        "datetime": alvo.isoformat(),
                        "agendado_em": agora.isoformat(),
                        "meet_link": meet_link,
                        "event_id": event_id,
                    })

                    with open(arquivo_reunioes, "w", encoding="utf-8") as f:
                        json.dump(reunioes_filtradas, f, indent=2, ensure_ascii=False)

                    data_fmt = alvo.strftime("%d/%m/%Y às %H:%M")
                    link_info = f"Link do Google Meet: {meet_link}" if meet_link else "O convite com o link chegará por e-mail."
                    instrucao_confirmacao = (
                        f"A reunião foi confirmada e criada no Google Calendar. "
                        f"Data: {data_fmt}. {link_info}. "
                        f"Escreva UMA mensagem de confirmação para o lead. "
                        f"Tom: profissional e direto, como uma SDR experiente encerrando o agendamento. "
                        f"Confirme a data/hora, mencione que o convite foi enviado por e-mail e inclua o link se houver. "
                        f"Sem emojis. Sem frases genéricas. Sem JSON, sem markdown, só o texto da mensagem."
                    )
                    gerar_e_enviar(instrucao_confirmacao, mensagens_contexto, headers)

                    # Lembrete 30min antes
                    segundos_total    = (alvo - agora).total_seconds()
                    segundos_lembrete = segundos_total - 1800

                    if segundos_lembrete > 0:
                        link_lembrete = meet_link or ""
                        instrucao_lembrete = (
                            f"Envie um lembrete ao lead de que a demo começa em 30 minutos, às {horario_reuniao}. "
                            + (f"Inclua o link: {link_lembrete}. " if link_lembrete else "")
                            + "Seja direto e profissional. Sem emojis. Sem JSON. Só o texto da mensagem."
                        )
                        def enviar_lembrete_gerado(phone, instrucao, headers):
                            llm_lembrete = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))
                            resp = llm_lembrete.invoke([
                                SystemMessage(content=get_contexto()),
                                SystemMessage(content=instrucao),
                            ])
                            enviar_mensagem(phone, resp.content.strip(), headers)

                        threading.Timer(segundos_lembrete, enviar_lembrete_gerado, args=(phone, instrucao_lembrete, headers)).start()
                        print(f"{GREEN}Lembrete agendado para {(alvo - timedelta(minutes=30)).strftime('%d/%m %H:%M')}{RESET}")

                    print(f"{GREEN}Reunião agendada: {alvo.strftime('%d/%m/%Y %H:%M')} | Meet: {meet_link}{RESET}")
                    return True

            except (json.JSONDecodeError, ValueError, KeyError) as e:
                print(f"{YELLOW}[verificar_reuniao] Resposta não é JSON de reunião: {type(e).__name__}: {e}{RESET}")

            return False

        def verificar_agendamento(resultado, headers) -> bool:
            """
            Verifica se a resposta é um agendamento de retorno.
            Retorna True se agendou (para parar o fluxo normal), False caso contrário.
            """
            resposta = resultado["messages"][-1]["content"]
            try:
                limpa = resposta.strip().strip("```json").strip("```").strip()
                parsed = json.loads(limpa)

                if parsed.get("acao") == "agendar_retorno":
                    horario_raw = parsed.get("horario", "")

                    horario = normalizar_horario(horario_raw)
                    if not horario:
                        print(f"{RED}Horário não reconhecido: {horario_raw}{RESET}")
                        return False

                    agora = datetime.now()
                    alvo = datetime.strptime(horario, "%H:%M").replace(
                        year=agora.year, month=agora.month, day=agora.day
                    )

                    if alvo <= agora:
                        alvo += timedelta(days=1)
                        print(f"{YELLOW}Horário já passou, agendando para amanhã: {alvo}{RESET}")

                    segundos = (alvo - agora).total_seconds()

                    gerar_e_enviar(
                        f"O lead pediu para ser contatado às {horario}. "
                        f"Confirme de forma natural que você retornará nesse horário. "
                        f"Sem emojis. Sem JSON. Só o texto da mensagem.",
                        [],
                        headers,
                    )
                    threading.Timer(segundos, create_answer, args=(phone, chatLid, data), kwargs={"is_recontato": True}).start()
                    print(f"{GREEN}Recontato agendado para {alvo.strftime('%d/%m %H:%M')} ({int(segundos)}s){RESET}")

                    # Retorno combinado → desativa follow-ups de inatividade
                    self.cancelar_followups(chatLid)
                    lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
                    try:
                        info_ret = {}
                        if os.path.exists(lead_info_path):
                            with open(lead_info_path, "r", encoding="utf-8") as f:
                                info_ret = json.load(f)
                        info_ret["necessita_followup"] = False
                        info_ret["motivo_followup"] = f"Retorno combinado agendado para {alvo.strftime('%d/%m %H:%M')} — follow-up de inatividade não necessário"
                        with open(lead_info_path, "w", encoding="utf-8") as f:
                            json.dump(info_ret, f, indent=2, ensure_ascii=False)
                    except Exception as e:
                        print(f"{RED}Erro ao desativar follow-up para retorno combinado: {e}{RESET}")

                    return True

            except (json.JSONDecodeError, ValueError, KeyError):
                pass

            return False

        def atualizar_info_lead(chatLid, mensagens, ultima_resposta):
            """
            Usa o LLM para extrair e atualizar informações estruturadas do lead
            com base na conversa até o momento. Salva em chats/{chatLid}/lead_info.json.
            """
            try:
                llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))

                prompt_extracao = """Com base na conversa abaixo entre um SDR da Btime e um lead, extraia as informações disponíveis e retorne APENAS um JSON com a estrutura:

{
  "nome": "nome do lead ou null",
  "email": "e-mail do lead ou null",
  "empresa": "nome da empresa ou null",
  "segmento_mercado": "segmento/setor de atuação ou null",
  "porte_empresa": "small/middle/enterprise ou null",
  "faturamento_aproximado": "faixa de faturamento mencionada ou null",
  "tamanho_time": "quantidade de funcionários/colaboradores da empresa (não alunos, clientes ou usuários) ou null",
  "sistemas_atuais": ["lista de sistemas/ERPs/ferramentas que usam"],
  "desafios_identificados": ["lista de dores/desafios mencionados pelo lead"],
  "produto_indicado": "Squad AI / SaaS Btime / Ambos / null",
  "motivo_segmentacao": "explicação clara dos sinais que levaram à segmentação",
  "estagio_conversa": "inicial/qualificando/qualificado/proposta/agendado",
  "reuniao_agendada": false,
  "necessita_followup": true,
  "motivo_followup": "explicação de por que o follow-up é ou não necessário",
  "atualizado_em": null
}

### Critérios de segmentação para o campo produto_indicado

Use os sinais abaixo para determinar o produto. Sempre que houver informação suficiente, preencha — não deixe null se der pra inferir.

**Squad AI** — indique quando houver sinais como:
- Projetos de TI parados, atrasados ou backlog acumulado
- Múltiplas áreas com problemas de tecnologia simultâneos
- Crescimento rápido com operação que não consegue escalar
- Custo de headcount crescendo sem ganho de produtividade
- Decisor é C-Level (CEO, COO, CTO) ou diretoria
- Empresa de médio porte (faturamento R$5M+) ou grande porte
- Falta de expertise interna em automações/integrações complexas

**SaaS Btime** — indique quando houver sinais como:
- Processos controlados em planilha ou papel
- Operação de campo sem visibilidade para o escritório
- Precisa padronizar como as tarefas são executadas
- Empresa small ou middle sem estrutura digital básica
- Decisor é gerência operacional ou o próprio dono
- Falta de integração entre sistemas simples

**Ambos** — indique quando o lead apresentar sinais dos dois lados: tem processos manuais (SaaS) e também backlog de TI ou múltiplas integrações complexas (Squad AI).

**null** — use apenas se a conversa ainda não teve informação suficiente para nenhuma inferência.

Para o campo motivo_segmentacao, explique os 2 ou 3 sinais concretos da conversa que levaram à conclusão. Ex: "Lead mencionou planilhas de controle e processo manual no campo → SaaS Btime. Não citou backlog de TI ou porte elevado."

### Campo necessita_followup

Preencha com true ou false com base no estado atual da conversa:

**ATENÇÃO: a última mensagem da conversa é o sinal mais importante. Leia com atenção antes de decidir.**

**false** (não precisa de follow-up) quando:
- A conversa terminou com uma despedida clara de qualquer lado ("até mais", "obrigada", "tchau", "abraço", "boa sorte", etc.)
- O SDR encerrou com uma frase de disponibilidade genérica ("estou à disposição", "qualquer coisa é só chamar") e o lead não deixou nenhuma pendência aberta
- Reunião/demo foi agendada com sucesso
- Lead pediu explicitamente para não ser contatado novamente
- Lead deixou claro que não tem interesse no produto
- Lead disse que vai pensar e entrará em contato quando quiser (iniciativa do lado dele)
- Lead já foi desqualificado (fora do perfil, sem budget, sem decisão)

**true** (precisa de follow-up) quando:
- Conversa ficou no meio sem nenhuma conclusão ou despedida
- Lead demonstrou interesse concreto mas não agendou e não houve encerramento
- Lead pediu para retomar depois sem data definida e sem despedida
- Lead parou de responder no meio de uma qualificação ativa (sem "obrigada", sem encerramento)
- Há uma pendência explícita aberta (ex: "vou te mandar o contrato", "te envio a proposta") que ainda não foi resolvida

**Regra de ouro:** se a última mensagem do lead foi "obrigada", "ok", "entendido", "até mais" ou qualquer variação de encerramento cortês → false. A conversa acabou.

### Regras críticas de extração

- **tamanho_time**: preencha SOMENTE com o número de funcionários/colaboradores/empregados da empresa. NÃO confunda com número de alunos, clientes, usuários, parceiros ou qualquer outro tipo de pessoa que não seja da equipe interna. Se o lead mencionar "300 alunos", o tamanho_time é null (não sabemos quantos funcionários tem). Se não foi mencionado explicitamente quantos funcionários/colaboradores a empresa tem, use null.
- **porte_empresa**: inferir com base no segmento, faturamento ou outros sinais, não pelo número de alunos/clientes.

Preencha apenas com o que foi explicitamente dito na conversa. Não invente informações. Para campos sem informação, use null ou lista vazia."""

                conversa_texto = "\n".join(
                    f"{'Lead' if m['role'] == 'user' else 'SDR'}: {m['content']}"
                    for m in mensagens
                ) + f"\nSDR: {ultima_resposta}"

                resposta = llm.invoke([
                    SystemMessage(content=prompt_extracao),
                    HumanMessage(content=conversa_texto),
                ])

                limpa = resposta.content.strip().strip("```json").strip("```").strip()
                info  = json.loads(limpa)
                info["atualizado_em"] = datetime.now().isoformat()

                lead_dir    = os.path.join("chats", chatLid)
                os.makedirs(lead_dir, exist_ok=True)
                arquivo_info = os.path.join(lead_dir, "lead_info.json")

                if os.path.exists(arquivo_info):
                    with open(arquivo_info, "r", encoding="utf-8") as f:
                        info_anterior = json.load(f)
                    # Campos que sempre devem refletir o estado mais recente da conversa
                    _sempre_atualizar = {"necessita_followup", "motivo_followup", "estagio_conversa", "atualizado_em"}
                    for key, val in info_anterior.items():
                        if key in _sempre_atualizar:
                            continue
                        if info.get(key) in (None, [], "") and val not in (None, [], ""):
                            # novo valor é vazio → mantém o anterior para não perder info
                            info[key] = val
                        # se o novo valor não é vazio, usa o novo (permite corrigir dados errados)

                with open(arquivo_info, "w", encoding="utf-8") as f:
                    json.dump(info, f, indent=2, ensure_ascii=False)

                print(f"{GREEN}Lead info atualizado: {arquivo_info}{RESET}")

            except Exception as e:
                print(f"{RED}Erro ao atualizar lead_info: {e}{RESET}")

        # --- Orquestração principal ---

        def create_answer(phone, chatLid, data, history_limit=50, is_recontato=False):
            self.pending_timers.pop(chatLid, None)
            self.pending_texts.pop(chatLid, None)

            print(f" {GREEN} > get_ai_response chamado para {chatLid} {RESET}")
            print(f"{RED}Anser_list: {self.answer_list}{RESET}")

            if is_recontato:
                headers = {"client-token": self.zapi_sec_token}
                llm = ChatOpenAI(model="gpt-4o-mini", api_key=os.environ.get("OPENAI_API_KEY"))

                file = os.path.join("chats", chatLid, "history.json")
                historico_recontato = []
                if os.path.exists(file):
                    with open(file, "r", encoding="utf-8") as f:
                        historico_recontato = json.load(f)
                mensagens_recontato = self.extrair_mensagens(historico_recontato, chatLid)[-10:]

                resposta_recontato = llm.invoke([
                    SystemMessage(content=get_contexto()),
                    *[
                        HumanMessage(content=m["content"]) if m["role"] == "user"
                        else AIMessage(content=m["content"])
                        for m in mensagens_recontato
                    ],
                    SystemMessage(content=(
                        "Você está retomando contato com o lead no horário combinado. "
                        "Escreva uma mensagem curta e natural retomando a conversa. "
                        "Sem emojis. Sem JSON. Só o texto da mensagem."
                    )),
                ])
                for parte in dividir_em_mensagens(resposta_recontato.content):
                    enviar_mensagem(phone, parte, headers)
                self.answer_list[phone] = data
                return

            if chatLid in self.answer_list:
                print(f" {YELLOW} > Respondendo para {chatLid} {RESET}")

                if True:
                    print(f" {BLUE} > Processo de resposta iniciado.\nChatLid: {chatLid} {RESET}")

                    workflow = StateGraph(dict)
                    workflow.add_node("processar", processar)
                    workflow.set_entry_point("processar")
                    workflow.add_edge("processar", END)
                    app = workflow.compile()

                    file = os.path.join("chats", chatLid, "history.json")
                    if os.path.exists(file):
                        with open(file, "r", encoding="utf-8") as f:
                            historico = json.load(f)
                    else:
                        historico = [{"data": data, "timestamp": data.get("momment", 0)}]

                    # Separa documentos (sempre incluídos) do restante (limitado por history_limit)
                    docs = [item for item in historico if isinstance(item.get("data", {}).get("document"), dict)]
                    nao_docs = [item for item in historico if not isinstance(item.get("data", {}).get("document"), dict)]
                    historico_filtrado = sorted(
                        docs + nao_docs[-history_limit:],
                        key=lambda x: x.get("timestamp", 0)
                    )
                    mensagens_formatadas = self.extrair_mensagens(historico_filtrado, chatLid)

                    # Injeta dados já conhecidos do lead no contexto
                    contexto = get_contexto()
                    lead_info_path = os.path.join("chats", chatLid, "lead_info.json")
                    if os.path.exists(lead_info_path):
                        with open(lead_info_path, "r", encoding="utf-8") as f:
                            lead_info_atual = json.load(f)
                        campos = {
                            "nome":  lead_info_atual.get("nome"),
                            "email": lead_info_atual.get("email"),
                        }
                        conhecidos = {k: v for k, v in campos.items() if v}
                        if conhecidos:
                            linhas = ", ".join(f"{k}: {v}" for k, v in conhecidos.items())
                            contexto += f"\n\nDados já conhecidos deste lead (não peça novamente): {linhas}."

                    # Injeta horários livres dos próximos 3 dias úteis no contexto
                    try:
                        from datetime import date
                        horarios_por_dia = {}
                        dia = datetime.now().date() + timedelta(days=1)
                        while len(horarios_por_dia) < 3:
                            if dia.weekday() < 5:  # seg–sex
                                livres = buscar_horarios_livres(dia)
                                if livres:
                                    horarios_por_dia[dia.strftime("%d/%m/%Y (%A)")] = livres
                            dia += timedelta(days=1)
                        if horarios_por_dia:
                            linhas_agenda = "\n".join(
                                f"- {d}: {', '.join(slots[:3])}"
                                for d, slots in horarios_por_dia.items()
                            )
                            contexto += f"\n\nHorários disponíveis na agenda (sugira apenas 2 ou 3 opções de forma natural, não liste tudo):\n{linhas_agenda}"
                    except Exception as e:
                        print(f"{RED}Erro ao buscar horários livres para contexto: {e}{RESET}")

                    resultado = app.invoke({
                        "messages": mensagens_formatadas,
                        "context": contexto,
                    })


                    atualizar_info_lead(chatLid, mensagens_formatadas, resultado["messages"][-1]["content"])

                    # Verifica se usuário ainda está digitando/gravando
                    if chatLid in self.composing_set:
                        print(f" {YELLOW} > {chatLid} ainda está digitando/gravando, resposta descartada {RESET}")
                        return

                    headers = {"client-token": self.zapi_sec_token}

                    if verificar_consulta_disponibilidade(resultado, headers, mensagens_formatadas):
                        return

                    if verificar_reuniao(resultado, headers, mensagens_formatadas):
                        return

                    if verificar_agendamento(resultado, headers):
                        return

                    resposta_completa = resultado["messages"][-1]["content"]

                    # Guard: nunca enviar JSON de ação como mensagem ao lead
                    try:
                        limpa = resposta_completa.strip().strip("```json").strip("```").strip()
                        parsed_guard = json.loads(limpa)
                        if isinstance(parsed_guard, dict) and "acao" in parsed_guard:
                            print(f"{RED}Resposta é um JSON de ação não tratado, descartando: {parsed_guard.get('acao')}{RESET}")
                            return
                    except (json.JSONDecodeError, ValueError):
                        pass


                    partes = dividir_em_mensagens(resposta_completa)

                    for i, parte in enumerate(partes):
                        if i > 0:
                            time.sleep(1.5)
                        enviar_mensagem(phone, parte, headers)
                        print(f"\n\n{GREEN}>Parte {i+1}/{len(partes)} enviada para {chatLid}{RESET}")

                    self.answer_list.pop(chatLid, None)
                    self.agendar_followups(phone, chatLid)

                else:
                    print(f" {RED}\n\n > Chat {chatLid} não permitido {RESET}")
                    return "Desculpe, mas não posso responder a este chat."
            else:
                print(f" {RED} > Nenhuma resposta encontrada para {chatLid} {RESET}")
                return "Desculpe, mas não encontrei uma resposta para esta mensagem."

        # Acumula texto das mensagens recebidas para calcular delay proporcional
        def _extrair_texto_data(d: dict) -> str:
            text = d.get("text", {})
            if isinstance(text, dict):
                return text.get("message", "")
            return str(text) if text else ""

        texto_nova = _extrair_texto_data(data)
        self.pending_texts[chatLid] = self.pending_texts.get(chatLid, "") + " " + texto_nova

        # Delay proporcional: 3s base + 0.04s por caractere acumulado, máx 12s
        total_chars = len(self.pending_texts[chatLid].strip())
        delay = min(3.0 + total_chars * 0.04, 12.0)

        # Debounce: cancela timer anterior do mesmo lead
        if chatLid in self.pending_timers:
            self.pending_timers[chatLid].cancel()
            print(f" {YELLOW} > Timer anterior cancelado para {chatLid} {RESET}")

        print(f" {YELLOW} > Delay proporcional: {delay:.1f}s ({total_chars} chars) para {chatLid} {RESET}")
        timer = threading.Timer(delay, create_answer, args=(phone, chatLid, data))
        self.pending_timers[chatLid] = timer
        timer.start()
