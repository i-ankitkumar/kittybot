"""The dashboard.

A small server-rendered app showing the kitty's balances, the pending
proposals and the recent expenses. It runs on localhost by default.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

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

#: Anything that looks like a Kittysplit link is scrubbed out of messages.
_SECRET_RE = re.compile(r"https?://(?:www\.)?kittysplit\.com/\S*", re.IGNORECASE)


def scrub(message: str) -> str:
    """Remove kitty URLs from a message before showing it to the user."""
    return _SECRET_RE.sub("[kitty link]", message or "")


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
        """
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            FLASH_COOKIE,
            json.dumps({"kind": kind, "message": scrub(message)}),
            httponly=True,
            samesite="strict",
            max_age=60,
            path="/",
        )
        return response

    def base_context(request: Request) -> dict:
        flash = read_flash(request)
        if flash:
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
        try:
            summary = engine.client.summary()
            expenses = engine.client.list_expenses()[:25]
        except Exception:
            summary = None
            expenses = []

        context = base_context(request)
        context.update(
            {
                "summary": summary,
                "expenses": expenses,
                "pending": store.pending_proposals(),
                "recent": store.list_proposals(limit=20),
                "symbol": (summary.currency_symbol if summary else "₹"),
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

    @app.get("/export.xlsx")
    async def export_xlsx():
        data = engine.client.export_xlsx()
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=kitty.xlsx"},
        )

    return app
