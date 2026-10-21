"""Aggregator for v1 API routes."""

from fastapi import APIRouter

from app.api.v1.endpoints import analysis, auth, conversations, documents, health

api_router_v1 = APIRouter()

api_router_v1.include_router(health.router, tags=["health"])
api_router_v1.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router_v1.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router_v1.include_router(analysis.router, prefix="/analysis", tags=["analysis"])
api_router_v1.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
