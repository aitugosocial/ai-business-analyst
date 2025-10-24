
# load the results page

# import the fastapi router function
from fastapi import APIRouter, Request

# allow fastapi enable html structure
from fastapi.responses import HTMLResponse

# allow fastapi append the static files
from fastapi.staticfiles import StaticFiles

# allow fastapi talk with the file structure
import os

# allow fastapi interact with the jinja templates 
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

router = APIRouter(
    prefix = "/results"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "assets")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
OUT_DIR = os.path.join(STATIC_DIR, "web")
next_static_dir = os.path.join(OUT_DIR, "_next")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

@router.get("/results", response_class= HTMLResponse)
async def serve_index(request: Request):
    """Serve result page"""
    return templates.TemplateResponse("results.html", {"request": request})