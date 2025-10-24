
# load the landing page

# import the fastAPI library into
from fastapi import FastAPI, Request, APIRouter

# import the function for rendering the HTML sites
from fastapi.responses import FileResponse, JSONResponse

# import the function for rendering the static files
from fastapi.staticfiles import StaticFiles

# import the function for rendering the Jinja files
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

import os

router = APIRouter(
    prefix = ""
)

# for the project's static folder done with react.js
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # get the absolute path of the file
BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)))  # get the current directory of the file
OUT_DIR = os.path.join(BASE_DIR, "web")     # get the absolute path of the out folder


@router.get("/{any_path:path}")
def serve_react(any_path: str):
    """Serve the React app for all routes."""
    index_path = os.path.join(OUT_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(status_code=404, content={"message": "index.html not found"})
'''
# for the project's static folder with next.js
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "assets")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
OUT_DIR = os.path.join(STATIC_DIR, "out")
next_static_dir = os.path.join(OUT_DIR, "_next")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

@router.get("/{full_path: path}")
async def serve_index(full_path: str):
    # get the path of the particular page we are to serve
    file_path = os.path.join(OUT_DIR, full_path)

    # check if the file exists and display it
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)
    """Serve the main index.html file"""
    index_path = os.path.join(OUT_DIR, "index.html")
    #if not os.path.exists(index_path):
      #  raise FileNotFoundError(f"index.html not found at {index_path}")
    return FileResponse(index_path)

@router.get("/")
async def serve_index():
    return FileResponse(os.path.join(OUT_DIR, "index.html"))

@router.get("/", response_class= HTMLResponse)
async def serve_index(request: Request):
    """Serve homepage"""
    return templates.TemplateResponse("index.html", {"request": request})
'''