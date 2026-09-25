#!/usr/bin/env python3
"""
Parser de e-mails do Google Alerts (encaminhados) — núcleo de lógica,
usado tanto pelo protótipo de linha de comando quanto pelo serviço HTTP.

Adaptado de samples/parse_alert.py: em vez de só ler de um arquivo .eml
em disco, expõe funções que trabalham em cima de bytes crus de e-mail,
para poderem ser chamadas a partir de um endpoint HTTP (main.py).
"""
import re
import email
from email.policy import default as default_policy
from urllib.parse import urlparse, parse_qs


def load_plaintext_part_from_bytes(raw_bytes: bytes) -> str:
    msg = email.message_from_bytes(raw_bytes, policy=default_policy)

    # O e-mail real é um forward: pode ter multipart/alternative direto,
    # ou (se o Gmail tiver citado o alerta original como texto dentro do
    # próprio corpo) o texto já vem embutido na parte text/plain externa.
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        raise ValueError("Nenhuma parte text/plain encontrada no e-mail.")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


def extract_target_url(google_redirect_url: str) -> str:
    """
    Decodifica https://www.google.com/url?rct=j&sa=t&url=<TARGET>&ct=ga&... -> TARGET
    """
    parsed = urlparse(google_redirect_url)
    qs = parse_qs(parsed.query)
    target = qs.get("url", [None])[0]
    return target or google_redirect_url


ARTICLE_URL_RE = re.compile(r"<(https://www\.google\.com/url\?[^>]+)>")


def clean_title_or_snippet(text: str) -> str:
    """
    Junta linhas quebradas em um único texto, remove marcadores de ênfase (*palavra*)
    e normaliza espaços.
    """
    joined = " ".join(line.strip() for line in text.splitlines() if line.strip())
    joined = joined.replace("*", "")
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


def find_chrome_start(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if line.strip().startswith("[image:"):
            return i
    return len(lines)


def find_chrome_end(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if "alerts/feedback?" in line:
            return i + 1
    return len(lines)


def parse_alert_text(text: str) -> dict:
    lines = text.splitlines()

    # 1) localizar o bloco "NEWS ... See more results"
    try:
        news_idx = next(i for i, l in enumerate(lines) if l.strip() == "NEWS")
    except StopIteration:
        raise ValueError("Marcador 'NEWS' não encontrado — layout inesperado.")

    end_idx = len(lines)
    for marker in ("See more results", "| Edit this alert", "Unsubscribe"):
        for i, l in enumerate(lines):
            if i > news_idx and marker in l:
                end_idx = min(end_idx, i)
                break

    # 2) topic + cadência ficam nas linhas ANTES de "NEWS"
    #    procurando de baixo para cima a partir de news_idx.
    pre_news = [l.strip() for l in lines[:news_idx] if l.strip()]
    # a última linha não-vazia antes de NEWS é a cadência+data,
    # a penúltima é o topic.
    cadence_line = pre_news[-1] if pre_news else ""
    topic = pre_news[-2] if len(pre_news) >= 2 else ""

    cadence_label, cadence_date_raw = None, None
    m = re.match(r"(.+?)\s*[⋅·]\s*(.+)", cadence_line)
    if m:
        cadence_label, cadence_date_raw = m.group(1).strip(), m.group(2).strip()
    else:
        cadence_date_raw = cadence_line

    # 3) bloco de notícias
    news_block_lines = lines[news_idx + 1 : end_idx]
    news_block_text = "\n".join(news_block_lines)

    url_matches = list(ARTICLE_URL_RE.finditer(news_block_text))
    if not url_matches:
        raise ValueError("Nenhum link de artigo (google.com/url?) encontrado.")

    targets = [extract_target_url(m.group(1)) for m in url_matches]

    # Descoberta real ao testar contra o e-mail de exemplo: alguns artigos
    # (ex.: o da Yahoo Finance nesta amostra) repetem o MESMO link de
    # redirecionamento duas vezes — uma antes do título, outra antes do
    # nome da fonte. Se não agrupar essas ocorrências, o parser conta o
    # mesmo artigo duas vezes. Agrupamos ocorrências consecutivas que
    # decodificam para a mesma URL alvo.
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(url_matches):
        j = i
        while j + 1 < len(url_matches) and targets[j + 1] == targets[i]:
            j += 1
        groups.append((i, j))
        i = j + 1

    articles = []
    pending_title_text = news_block_text[: url_matches[0].start()]

    for g_idx, (i, j) in enumerate(groups):
        article_url = targets[i]

        # título: texto antes da 1ª ocorrência do grupo + texto entre
        # ocorrências repetidas dentro do mesmo grupo (na amostra, é
        # justamente aí que o título "de verdade" aparece).
        title_parts = [pending_title_text]
        for k in range(i, j):
            title_parts.append(news_block_text[url_matches[k].end() : url_matches[k + 1].start()])
        title_text = " ".join(title_parts)

        # segmento após a última ocorrência do grupo, até o próximo grupo (ou fim do bloco)
        seg_start = url_matches[j].end()
        if g_idx + 1 < len(groups):
            seg_end = url_matches[groups[g_idx + 1][0]].start()
        else:
            seg_end = len(news_block_text)
        segment = news_block_text[seg_start:seg_end]
        seg_lines = segment.splitlines()

        chrome_start = find_chrome_start(seg_lines)
        source_and_snippet_lines = seg_lines[:chrome_start]
        chrome_end = find_chrome_end(seg_lines)
        next_title_lines = seg_lines[chrome_end:]

        # primeira linha não vazia = nome da fonte; restante = snippet
        clean_sns = [l for l in source_and_snippet_lines if l.strip()]
        source_name = clean_sns[0].strip() if clean_sns else ""
        snippet = clean_title_or_snippet("\n".join(clean_sns[1:])) if len(clean_sns) > 1 else ""

        articles.append(
            {
                "title": clean_title_or_snippet(title_text),
                "url": article_url,
                "source_name": source_name,
                "snippet": snippet,
            }
        )

        pending_title_text = "\n".join(next_title_lines)

    return {
        "topic": topic,
        "cadence_label": cadence_label,
        "cadence_date_raw": cadence_date_raw,
        "articles": articles,
    }


def parse_eml_bytes(raw_bytes: bytes) -> dict:
    """Ponto de entrada único usado pelo endpoint HTTP: e-mail cru (bytes) -> dict estruturado."""
    text = load_plaintext_part_from_bytes(raw_bytes)
    return parse_alert_text(text)
