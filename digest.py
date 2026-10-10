#!/usr/bin/env python3
"""
Ежедневный дайджест новых статей по психиатрии и психотерапии.

Источники: PubMed (E-utilities) и Europe PMC (REST API).
Только стандартная библиотека Python 3.9+, установка пакетов не нужна.

Настройка через переменные окружения:
  SMTP_HOST, SMTP_PORT (по умолчанию 587), SMTP_USER, SMTP_PASSWORD
  MAIL_FROM, MAIL_TO          - адреса отправителя и получателя
  NCBI_API_KEY                - необязательно, повышает лимит запросов к PubMed
  DAYS_BACK                   - за сколько дней смотреть (по умолчанию 2)

Без SMTP-настроек скрипт работает в тестовом режиме: пишет дайджест в файл
в папке digests/ и не записывает статьи в базу, чтобы их можно было повторно проверить.
"""
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import textwrap
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "seen.sqlite3"
OUT_DIR = BASE_DIR / "digests"

DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))  # небольшое перекрытие, дубликаты отсекаются базой
MAX_RESULTS = 200
NCBI_KEY = os.environ.get("NCBI_API_KEY", "")
PAUSE = 0.12 if NCBI_KEY else 0.4  # пауза между запросами к NCBI

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

# Поисковые запросы. При необходимости добавьте журналы или термины.
PUBMED_QUERY = ("(psychiatry[MeSH Terms] OR psychotherapy[MeSH Terms] "
                "OR psychiatr*[tiab] OR psychotherap*[tiab])")
# Из Europe PMC берём только препринты (medRxiv, bioRxiv, PsyArXiv и др.): журналы уже дал PubMed
EPMC_QUERY = "(TITLE_ABS:psychiatr* OR TITLE_ABS:psychotherap*) AND SRC:PPR"


def get(url, params=None):
    """HTTP GET с повторными попытками."""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(full, headers={"User-Agent": "psych-digest/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def eutils(endpoint, params):
    if NCBI_KEY:
        params["api_key"] = NCBI_KEY
    data = get(EUTILS + endpoint, params)
    time.sleep(PAUSE)
    return data


def text_of(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")  # убрать HTML-теги из Europe PMC
    return re.sub(r"\s+", " ", s).strip()


def normalize(source, title, authors, journal, pub_date, doi, pmid, abstract, url):
    return {
        "source": source,
        "title": clean(title),
        "authors": [clean(a) for a in authors if clean(a)],
        "journal": clean(journal),
        "date": clean(pub_date),
        "doi": (doi or "").strip(),
        "pmid": (pmid or "").strip(),
        "abstract": html.unescape(re.sub(r"<[^>]+>", "", abstract or "")).strip(),
        "url": url,
    }


# ---------- PubMed ----------

def parse_pubmed(art):
    cit = art.find("MedlineCitation")
    a = cit.find("Article")
    pmid = text_of(cit.find("PMID"))
    title = text_of(a.find("ArticleTitle"))
    abstract = "\n".join(
        (f"{at.get('Label')}: " if at.get("Label") else "") + text_of(at)
        for at in a.findall("Abstract/AbstractText")
    )
    authors = []
    for au in a.findall("AuthorList/Author"):
        collective = text_of(au.find("CollectiveName"))
        if collective:
            authors.append(collective)
        else:
            name = " ".join(filter(None, [text_of(au.find("ForeName")), text_of(au.find("LastName"))]))
            authors.append(name)
    pd = a.find("Journal/JournalIssue/PubDate")
    if pd is not None and len(pd):
        pub_date = " ".join(filter(None, (text_of(c) for c in pd)))
    else:
        pub_date = text_of(pd)
    doi = next((text_of(x) for x in art.findall("PubmedData/ArticleIdList/ArticleId")
                if x.get("IdType") == "doi"), "")
    return normalize("PubMed", title, authors, a.findtext("Journal/Title") or "",
                     pub_date, doi, pmid, abstract,
                     f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "")


def pubmed_items(days):
    params = {"db": "pubmed", "term": PUBMED_QUERY, "reldate": days,
              "datetype": "edat", "retmax": MAX_RESULTS, "retmode": "json"}
    ids = json.loads(eutils("esearch.fcgi", params))["esearchresult"]["idlist"]
    out = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        root = ET.fromstring(eutils("efetch.fcgi", {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"}))
        out += [parse_pubmed(a) for a in root.findall("PubmedArticle")]
    return out


# ---------- Europe PMC ----------

def europepmc_items(days):
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    query = f"{EPMC_QUERY} AND FIRST_PDATE:[{start} TO {end}]"
    data = json.loads(get(EPMC_URL, {"query": query, "format": "json",
                                     "resultType": "core", "pageSize": 200}))
    out = []
    for r in data.get("resultList", {}).get("result", []):
        pmid = r.get("pmid", "")
        doi = r.get("doi", "")
        if pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            url = f"https://europepmc.org/article/{r.get('source')}/{r.get('id')}"
        authors = (r.get("authorString") or "").split(",")
        out.append(normalize("Препринт", r.get("title", ""), authors,
                             r.get("bookOrReportDetails", {}).get("publisher", "")
                             or r.get("journalTitle", "") or "Препринт",
                             r.get("firstPublicationDate", ""),
                             doi, pmid, r.get("abstractText", ""), url))
    return out


# ---------- Дедупликация ----------

def keys_of(item):
    ks = []
    if item["pmid"]:
        ks.append("pmid:" + item["pmid"])
    if item["doi"]:
        ks.append("doi:" + item["doi"].lower())
    ks.append("title:" + re.sub(r"[^a-z0-9]", "", item["title"].lower())[:120])
    return ks


def is_seen(con, key):
    return con.execute("SELECT 1 FROM seen WHERE k = ?", (key,)).fetchone() is not None


# ---------- Вывод и отправка ----------

def render(items, day):
    lines = [f"Новые публикации по психиатрии и психотерапии: {day}",
             f"Всего новых статей: {len(items)}", ""]
    for n, it in enumerate(items, 1):
        authors = ", ".join(it["authors"][:6]) + (" и др." if len(it["authors"]) > 6 else "")
        lines += [
            f"{n}. {it['title']}",
            f"   Авторы: {authors or '—'}",
            f"   Журнал: {it['journal'] or '—'} | Дата: {it['date'] or '—'} | Источник: {it['source']}",
            f"   Ссылка: {it['url'] or '—'}",
        ]
        if it["doi"]:
            lines.append(f"   DOI: {it['doi']}")
        lines.append("   Аннотация:")
        lines.append(textwrap.indent(it["abstract"] or "нет аннотации", "   "))
        lines.append("")
    return "\n".join(lines)


def render_html(items, day, note=""):
    def esc(s):
        return html.escape(s or "")

    def block(n, it):
        authors = ", ".join(it["authors"][:6]) + (" и др." if len(it["authors"]) > 6 else "")
        meta = " · ".join(filter(None, [authors, it["journal"], it["date"]]))
        abstract = esc(it["abstract"]).replace("\n", "<br>") or "<i>Аннотация отсутствует</i>"
        return (f'<div style="margin:0 0 22px">'
                f'<div style="font-size:16px;font-weight:bold">{n}. '
                f'<a href="{esc(it["url"])}" style="color:#1a4f8b;text-decoration:none">{esc(it["title"])}</a></div>'
                f'<div style="color:#666;font-size:13px;margin:3px 0 6px">{esc(meta)}</div>'
                f'<div style="font-size:14px;line-height:1.45">{abstract}</div></div>')

    journals = [it for it in items if it["source"] == "PubMed"]
    preprints = [it for it in items if it["source"] != "PubMed"]
    parts = [f'<div style="font-family:Arial,sans-serif;max-width:780px;color:#222">',
             f'<h2 style="margin:0 0 4px">Психиатрия и психотерапия</h2>',
             f'<div style="color:#666;margin-bottom:18px">{day} · новых статей: {len(items)}</div>']
    if note:
        parts.append(f'<div style="background:#fff4e5;padding:8px 12px;margin-bottom:16px">{esc(note)}</div>')
    n = 0
    for title, group in (("Журнальные статьи (PubMed)", journals), ("Препринты", preprints)):
        if group:
            parts.append(f'<h3 style="border-bottom:1px solid #ddd;padding-bottom:4px">{title} — {len(group)}</h3>')
            for it in group:
                n += 1
                parts.append(block(n, it))
    if not items:
        parts.append("<p>За этот период новых статей не найдено.</p>")
    parts.append('<div style="color:#999;font-size:12px;margin-top:24px">Источники: PubMed, Europe PMC (препринты)</div></div>')
    return "".join(parts)


def smtp_configured():
    keys = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO"]
    return all(os.environ.get(k) for k in keys)


def send_email(subject, body, html_body=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:  # SSL с самого начала соединения (Яндекс, Mail.ru и др.)
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
    else:            # порт 587: обычное соединение, затем STARTTLS
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls(context=ctx)
    with server as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


# ---------- Основной сценарий ----------

def main():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, added TEXT)")

    items, failed = [], []
    for name, fn in (("PubMed", pubmed_items), ("Europe PMC", europepmc_items)):
        try:
            found = fn(DAYS_BACK)
            print(f"{name}: найдено {len(found)}", file=sys.stderr)
            items += found
        except Exception as e:
            failed.append(name)
            print(f"{name}: ошибка: {e}", file=sys.stderr)
    if len(failed) == 2:
        sys.exit("Оба источника недоступны, дайджест не сформирован.")
    note = f"Источник недоступен сегодня: {', '.join(failed)}. Список может быть неполным." if failed else ""

    fresh, local = [], set()
    for it in items:
        ks = keys_of(it)
        if not it["title"] or any(k in local for k in ks) or any(is_seen(con, k) for k in ks):
            continue
        fresh.append(it)
        local.update(ks)
    fresh.sort(key=lambda it: it["source"] != "PubMed")  # сначала журналы, затем препринты

    today = date.today().isoformat()
    body = (note + "\n\n" if note else "") + render(fresh, today)
    html_body = render_html(fresh, today, note)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"digest_{today}.txt").write_text(body, encoding="utf-8")
    (OUT_DIR / f"digest_{today}.html").write_text(html_body, encoding="utf-8")
    print(f"Новых статей: {len(fresh)}. Файлы в {OUT_DIR}", file=sys.stderr)

    if not smtp_configured():
        print("SMTP не настроен: письмо не отправлено, база не обновлена (тестовый режим).",
              file=sys.stderr)
        return

    send_email(f"Психиатрия и психотерапия: новых статей {len(fresh)} ({today})", body, html_body)
    con.executemany("INSERT OR IGNORE INTO seen (k, added) VALUES (?, ?)",
                    [(k, today) for it in fresh for k in keys_of(it)])
    con.commit()
    print("Дайджест отправлен.", file=sys.stderr)


if __name__ == "__main__":
    main()
