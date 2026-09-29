"""uvicorn entry point: uvicorn main:app"""
import os

from app import create_app

app = create_app(os.environ.get("DBV_CONFIG", "config.yaml"), os.environ.get("DBV_AUDIT", "audit.log"), os.environ)
