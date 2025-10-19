
# import the fastAPI library into
from fastapi import FastAPI, Request

# import the function for rendering the HTML sites
from fastapi.responses import HTMLResponse

# import the function for rendering the static files
from fastapi.staticfiles import StaticFiles

# import the function for rendering the Jinja files
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

import os

app = FastAPI()

# create a directory get function for the static files
BASE_DIR= os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# env = Environment(loader=FileSystemLoader("templates"), cache_size=0)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
# TEMPLATES_DIR.env = env

# mount the static files imported into the project
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# recognize the html template sites for interaction
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# create the path for the react folder
# function for the home page
@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """Serve homepage"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/result", response_class=HTMLResponse)
async def serve_result(request: Request):
    """Serve result page"""
    return templates.TemplateResponse("result.html", {"request": request})

@app.get("/api/hello")
async def api_hello():
    """Simple API test"""
    return {"message": "Hello from FastAPI"}

@app.exception_handler(404)
async def not_found(request: Request, exc):
    """Return index.html for unknown routes (useful for SPA)"""
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_path):
        return templates.TemplateResponse("index.html", {"request": request})
    return {"error": "Page not found"}
