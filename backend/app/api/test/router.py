from fastapi import APIRouter
from app.api.test import test

testrouter  = APIRouter()

testrouter.include_router(test.router)