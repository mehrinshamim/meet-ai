"""
main.py — FastAPI application entry point.

This file does three things:
  1. Creates the FastAPI app instance
  2. Adds CORS middleware (so the browser frontend can call the API)
  3. Mounts all routers under /api

Why a /api prefix?
  The frontend HTML files will be served from the same domain (or localhost).
  Prefixing all API routes with /api makes it trivially easy to distinguish
  "is this a page request?" from "is this an API call?" — useful for nginx
  config, CORS rules, and debugging.

Why CORSMiddleware with allow_origins=["*"]?
  During development, the frontend runs on a different port (e.g. file:// or
  localhost:5500) than the backend (localhost:8000).  Browsers block these
  cross-origin requests by default.  The CORS middleware adds the headers that
  tell browsers it's safe to proceed.
  For production, replace ["*"] with your actual domain.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routes import meetings

app = FastAPI(
    title="MeetAI",
    description="AI-powered meeting transcript analysis",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten this in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all meeting/project/stats routes under /api
app.include_router(meetings.router, prefix="/api")
