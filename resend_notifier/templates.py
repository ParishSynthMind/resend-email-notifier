"""Email templates: one subject, one HTML part and one plain-text part per email.

A template called ``order_confirmation`` is three files in the template folder:

    order_confirmation.subject.txt
    order_confirmation.html          (autoescaped: customer data cannot inject HTML)
    order_confirmation.txt           (plain text, not escaped)

Undefined variables raise an error instead of rendering as blanks, so a
missing order number is caught before an email goes out.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Union

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound
from jinja2.exceptions import TemplateError as JinjaTemplateError

from .errors import TemplateError


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


class TemplateRenderer:
    def __init__(self, template_dir: Union[str, Path]):
        self.template_dir = Path(template_dir)
        loader = FileSystemLoader(str(self.template_dir))
        self._html_env = Environment(loader=loader, autoescape=True,
                                     undefined=StrictUndefined, keep_trailing_newline=True)
        self._text_env = Environment(loader=loader, autoescape=False,
                                     undefined=StrictUndefined, keep_trailing_newline=True)

    def render(self, name: str, context: Dict[str, Any]) -> RenderedEmail:
        try:
            subject = self._text_env.get_template(f"{name}.subject.txt").render(**context)
            html = self._html_env.get_template(f"{name}.html").render(**context)
            text = self._text_env.get_template(f"{name}.txt").render(**context)
        except TemplateNotFound as exc:
            raise TemplateError(f"template file not found: {exc.name}") from exc
        except JinjaTemplateError as exc:
            raise TemplateError(f"template '{name}' failed to render: {exc}") from exc
        # Subject is a single header line: collapse whitespace so a value with a
        # newline in it can never add a header.
        subject = " ".join(subject.split())
        if not subject:
            raise TemplateError(f"template '{name}' rendered an empty subject")
        return RenderedEmail(subject=subject, html=html, text=text)
