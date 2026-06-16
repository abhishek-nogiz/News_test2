from __future__ import annotations

import re

from .models import DraftArticle, WriterInput


class NewsroomFormatter:
    def format(self, draft: DraftArticle, writer_input: WriterInput) -> DraftArticle:
        html = draft.html.strip()
        if not html.startswith("<article"):
            html = f'<article class="trend-agent-post" data-story-id="{writer_input.story_id}" data-run-id="">{html}</article>'
        html = re.sub(r"<h1>(.*?)</h1>", lambda match: f"<h1>{match.group(1).rstrip('.!?')}</h1>", html, count=1, flags=re.IGNORECASE | re.DOTALL)
        if "<section data-type=\"sources\">" not in html:
            html = html.replace("</article>", "<section data-type=\"sources\"><ul></ul></section></article>")
        html = re.sub(r"\n{3,}", "\n\n", html)
        draft.html = html.strip() + "\n"
        draft.markdown = draft.markdown.strip() + "\n"
        return draft