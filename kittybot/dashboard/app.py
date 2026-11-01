"""The dashboard.

A small server-rendered app showing the kitty's balances, the pending
proposals and the recent expenses. It runs on localhost by default.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..approvals import ApprovalEngine
from ..config import Config
from ..models import ApprovalStatus
from ..store import Store

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

#: Flash messages travel in a short-lived cookie rather than the query string.
#: A Kittysplit error can contain the kitty URL, and that URL is a secret
#: credential — it must not end up in the address bar or in browser history.
FLASH_COOKIE = "kittybot_flash"

#: Re-exported so the dashboard and the Telegram bot scrub identically.
from ..scrub import scrub  # noqa: E402  (kept next to its use)


def source_label(source: str) -> str:
    """A consistent, human-readable name for a marketplace or ``manual``."""
    labels = {
        "instamart": "Instamart",
        "zepto": "Zepto",
        "blinkit": "Blinkit",
        "bigbasket": "BigBasket",
        "manual": "Manual",
    }
    return labels.get((source or "").lower(), (source or "unknown").title())


def _auth_dependency(token: str):
    """Build a dependency that enforces HTTP Basic auth with ``token``.

    The dashboard can approve expenses on the kitty, so it must not be an
    open door if it is reachable by anything other than this machine. Any
    username is accepted; the token is the password.
    """
    from fastapi import Depends
    from fastapi.security import HTTPBasic, HTTPBasicCredentials

    basic = HTTPBasic(auto_error=False)

    def require_auth(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> bool:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        supplied = (credentials.password or "").strip()
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Basic"},
            )
        return True

    return require_auth


def create_app(config: Config, engine: ApprovalEngine, store: Store) -> FastAPI:
    """Build the dashboard app."""
    token = (config.dashboard.token or "").strip()
    dependencies = [Depends(_auth_dependency(token))] if token else []

    app = FastAPI(
        title="kittybot",
        docs_url=None,
        redoc_url=None,
        dependencies=dependencies,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Amounts read better grouped: 18500.00 is hard to scan, 18,500.00 is not.
    # None means "you are not involved in this expense", which is a real
    # state in a kitty with more than two people, so it must not blow up.
    templates.env.filters["money"] = lambda value: (
        "-" if value is None else f"{value:,.2f}"
    )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def party_names() -> dict[int, str]:
        """Map kitty party ids to participant names."""
        try:
            # participant_ids() is name -> id; the templates need id -> name.
            return {
                party_id: name
                for name, party_id in engine.client.participant_ids().items()
            }
        except Exception:
            return {}

    def read_flash(request: Request) -> dict | None:
        raw = request.cookies.get(FLASH_COOKIE)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def redirect_with_flash(url: str, kind: str, message: str) -> RedirectResponse:
        """Redirect after a POST.

        303 See Other, not the 307 default: a 307 preserves the POST method,
        so the browser would re-POST to a GET-only page and land on a raw
        JSON "405 Method Not Allowed".

        The message records the page it belongs to. A flash set in one tab
        must not show up on an unrelated proposal in another tab.
        """
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLASH_COOKIE,
            json.dumps({"kind": kind, "message": scrub(message), "for": url}),
            httponly=True,
            samesite="strict",
            max_age=60,
            path="/",
        )
        return response

    def base_context(request: Request) -> dict:
        flash = read_flash(request)
        if flash:
            intended = flash.get("for") or ""
            if intended and request.url.path != urlparse(intended).path:
                # Not the page this message is about. Leave it in the cookie
                # so it still reaches the right page, and show nothing here.
                flash = None
            else:
                # One-shot: clear it so a refresh does not repeat the message.
                request.cookies.clear()
        return {
            "config": config,
            "symbol": "₹",
            "flash": flash,
            "source_label": source_label,
            "party_names": party_names(),
        }

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        kitty_error = None
        try:
            summary = engine.client.summary()
            expenses = engine.client.list_expenses()[:25]
        except Exception as exc:
            # A blanket except used to turn "Kittysplit is unreachable" into
            # a page that reads like a perfectly normal, empty kitty. On a
            # money dashboard that is the dangerous kind of empty: the user
            # sees a zero total and concludes they are square.
            summary = None
            expenses = []
            kitty_error = scrub(str(exc)) or exc.__class__.__name__

        context = base_context(request)
        context.update(
            {
                "summary": summary,
                "expenses": expenses,
                "pending": store.pending_proposals(),
                "recent": store.list_proposals(limit=20),
                "symbol": (summary.currency_symbol if summary else "₹"),
                "kitty_error": kitty_error,
            }
        )
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/pending", response_class=HTMLResponse)
    async def pending_page(request: Request):
        context = base_context(request)
        context["pending"] = store.pending_proposals()
        return templates.TemplateResponse(request, "pending.html", context)

    @app.get("/proposal/{proposal_id}", response_class=HTMLResponse)
    async def proposal_page(request: Request, proposal_id: int):
        draft = store.get_proposal(proposal_id)
        if draft is None:
            return redirect_with_flash("/pending", "error", "That proposal no longer exists.")
        context = base_context(request)
        context["proposal"] = draft
        return templates.TemplateResponse(request, "proposal.html", context)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    @app.post("/proposal/{proposal_id}/approve")
    async def approve_proposal(request: Request, proposal_id: int):
        form = await request.form()
        spec = (form.get("split") or "").strip()
        try:
            if spec:
                draft = engine.approve_with_spec(proposal_id, spec)
            else:
                draft = engine.approve(proposal_id)
        except Exception as exc:
            # Never swallow this. Record it on the proposal so the reason is
            # visible in Recent Activity, and tell the user what went wrong.
            message = str(exc) or exc.__class__.__name__
            # Only mark it failed if it was still pending. A double-submit
            # arrives after the first request already decided it, and
            # overwriting that status would make the dashboard report a
            # successful expense as failed -- while the kitty entry it
            # created is sitting there.
            current = store.get_proposal(proposal_id)
            if current is not None and current.status is ApprovalStatus.PENDING:
                store.decide(proposal_id, ApprovalStatus.FAILED, error=message)
            return redirect_with_flash("/pending", "error", message)

        if draft.status is ApprovalStatus.FAILED:
            return redirect_with_flash(
                "/pending",
                "error",
                draft.error or "The expense could not be written to the kitty.",
            )
        return redirect_with_flash(
            "/pending",
            "success",
            f"Added {draft.description} ({draft.split_summary()}).",
        )

    @app.post("/proposal/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: int):
        draft = engine.reject(proposal_id)
        if draft is None:
            return redirect_with_flash("/pending", "error", "That proposal no longer exists.")
        return redirect_with_flash("/pending", "success", f"Rejected {draft.description}.")

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception):
        """Render a styled 404 instead of FastAPI's raw JSON."""
        context = base_context(request)
        return templates.TemplateResponse(request, "404.html", context, status_code=404)

    @app.get("/export.xlsx")
    async def export_xlsx():
        data = engine.client.export_xlsx()
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=kitty.xlsx"},
        )

    return app
