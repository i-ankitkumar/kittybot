"""The dashboard.

A small server-rendered app showing the kitty's balances, the pending
proposals and the recent expenses. It runs on localhost by default.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..approvals import ApprovalEngine
from ..config import Config
from ..models import ApprovalStatus, ExpenseDraft
from ..store import Store

TEMPLATES_DIR = __import__("pathlib").Path(__file__).parent / "templates"


def create_app(config: Config, engine: ApprovalEngine, store: Store) -> FastAPI:
    """Build the dashboard app."""
    app = FastAPI(title="kittybot", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        try:
            summary = engine.client.summary()
            expenses = engine.client.list_expenses()[:25]
        except Exception:
            summary = None
            expenses = []

        pending = store.pending_proposals()
        recent = store.list_proposals(limit=20)

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "config": config,
                "summary": summary,
                "expenses": expenses,
                "pending": pending,
                "recent": recent,
                "symbol": (summary.currency_symbol if summary else "₹"),
            },
        )

    @app.get("/pending", response_class=HTMLResponse)
    async def pending_page(request: Request):
        return templates.TemplateResponse(
            request,
            "pending.html",
            {"pending": store.pending_proposals(), "symbol": "₹"},
        )

    @app.get("/proposal/{proposal_id}")
    async def proposal_page(request: Request, proposal_id: int):
        draft = store.get_proposal(proposal_id)
        if draft is None:
            return RedirectResponse("/pending")
        return templates.TemplateResponse(
            request, "proposal.html", {"proposal": draft, "symbol": "₹"}
        )

    @app.post("/proposal/{proposal_id}/approve")
    async def approve_proposal(request: Request, proposal_id: int):
        form = await request.form()
        spec = (form.get("split") or "").strip()
        try:
            if spec:
                engine.approve_with_spec(proposal_id, spec)
            else:
                engine.approve(proposal_id)
        except Exception:
            pass
        return RedirectResponse("/pending")

    @app.post("/proposal/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: int):
        engine.reject(proposal_id)
        return RedirectResponse("/pending")

    @app.get("/export.xlsx")
    async def export_xlsx():
        from fastapi.responses import Response

        data = engine.client.export_xlsx()
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=kitty.xlsx"},
        )

    return app
