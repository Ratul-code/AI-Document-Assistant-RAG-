from fastapi import APIRouter
from pydantic import BaseModel

class TestResponse(BaseModel):
    """Schema for the health endpoint response."""

    name: str


router = APIRouter()
@router.get("/", response_model=TestResponse)
def name():
    return TestResponse(name="ratul")