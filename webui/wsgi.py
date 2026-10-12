"""Entry point for waitress: waitress-serve wsgi:app"""
from app import create_app

app = create_app()
