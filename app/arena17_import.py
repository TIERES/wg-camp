import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class Arena17InfoParser(HTMLParser):
    """Extract the championship title and label/value pairs from Arena17's Info tab."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.info = {}
        self.settings = {}
        self._heading = ""
        self._capture = None
        self._capture_depth = 0
        self._text = []
        self._pending_label = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "h3" and not self.title:
            self._begin_capture("title")
        elif tag == "h4":
            self._begin_capture("heading")
        elif tag == "label":
            self._begin_capture("label")
        elif tag == "div" and self._pending_label and "card-text" in attributes.get("class", "").split():
            self._begin_capture("value")
        elif self._capture == "value" and tag == "div":
            self._capture_depth += 1

    def handle_endtag(self, tag):
        if self._capture == "value" and tag == "div":
            self._capture_depth -= 1
            if self._capture_depth == 0:
                value = self._finish_capture()
                target = self.settings if self._heading.casefold() == "configurações" else self.info
                if value:
                    target[self._pending_label] = value
                self._pending_label = None
        elif self._capture == "title" and tag == "h3":
            self.title = self._finish_capture()
        elif self._capture == "heading" and tag == "h4":
            self._heading = self._finish_capture()
        elif self._capture == "label" and tag == "label":
            self._pending_label = self._finish_capture()

    def handle_data(self, data):
        if self._capture:
            self._text.append(data)

    def _begin_capture(self, kind):
        self._capture = kind
        self._capture_depth = 1
        self._text = []

    def _finish_capture(self):
        value = " ".join(" ".join(self._text).split())
        self._capture = None
        self._capture_depth = 0
        self._text = []
        return value


def championship_id_from_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"arena17.com", "www.arena17.com"}:
        raise ValueError("Informe uma URL válida de campeonato do Arena17.")
    match = re.fullmatch(r"/(?:campeonato|torneo)/(\d+)/?", parsed.path)
    if not match:
        raise ValueError("A URL deve ter o formato https://www.arena17.com/campeonato/12345.")
    return match.group(1)


def _fetch_html(url):
    request = Request(url, headers={"User-Agent": "Arena17-Downloads/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            return response.read(2 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8")
    except (HTTPError, URLError, TimeoutError, UnicodeError) as error:
        raise ValueError("Não foi possível consultar este campeonato no Arena17.") from error


def import_championship(url):
    championship_id = championship_id_from_url(url)
    championship_url = f"https://www.arena17.com/campeonato/{championship_id}"
    detail_url = f"https://www.arena17.com/campeonato/detalhe/{championship_id}"
    title_parser = Arena17InfoParser()
    title_parser.feed(_fetch_html(championship_url))
    parser = Arena17InfoParser()
    parser.feed(_fetch_html(detail_url))
    title = title_parser.title or parser.title
    if not title:
        raise ValueError("Não foi possível identificar o campeonato informado.")

    description = "\n".join(f"{label}: {value}" for label, value in parser.settings.items())
    relevant_labels = ("Status", "Administrador", "Jogo", "Plataforma", "Formato", "Equipes", "Taxa de Inscrição")
    details = {label: parser.info[label] for label in relevant_labels if label in parser.info}
    league = next((value for label, value in parser.info.items() if label.casefold() == "liga"), "")
    return {"name": title, "league_name": league, "description": description, "details": details}
