
# import the fastAPI library into
from fastapi import FastAPI, Request, HTTPException

# import the function for rendering the HTML sites
from fastapi.responses import FileResponse

# import the function for rendering the static files
from fastapi.staticfiles import StaticFiles

# import the function for rendering the Jinja files
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

# import the database connection file and models from the containing folder
from db.connections import engine, SessionLocal, Base
import db.models

from fastapi.middleware.cors import CORSMiddleware

import os

# import the router page
from api.routes import index, signup, login, analyzer


app = FastAPI(debug = True)


# Enable CORS for (React form requests)
app.add_middleware(
    CORSMiddleware,
    allow_origins = ["*"],
    allow_credentials = True,
    allow_methods = ["*"],
    allow_headers = ["*"]
)

# Path to the React build
BASE_DIR = os.path.dirname(os.path.dirname((os.path.abspath(__file__))))
out_dir = os.path.join(BASE_DIR, "web")

# call the database connection and create all the tables
db.models.Base.metadata.create_all(bind = engine)


app.include_router(index.router)
app.include_router(signup.router)
app.include_router(login.router)
app.include_router(analyzer.router)


# Serve static assets (JS, CSS, etc.)
if os.path.exists(os.path.join(out_dir, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(out_dir, "assets")), name="assets")

# Catch-all route for React app (MUST be last)
@app.get("/{full_path:path}")
async def serve_react_app(full_path: str, request: Request):

    excluded_prefixes = ("api", "login", "token", "signup", "analyzer", "docs", "redoc", "openapi.json")
    if full_path.startswith(excluded_prefixes) or full_path == "":
        raise HTTPException(status_code=404, detail="API route not served as frontend")

    # Serve actual static file if exists
    file_path = os.path.join(out_dir, full_path)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)

    # Otherwise, serve index.html (React entry point)
    index_path = os.path.join(out_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)

    return {"error": "Frontend not found"}
# Serve React entry (index.html)
#@app.get("/{full_path:path}")
#async def serve_react(full_path: str):
#    return FileResponse(os.path.join(out_dir, "index.html"))
'''
# Serve index.html for all other routes (React Router fallback)
@app.get("/{full_path:path}")
async def serve_react_app(full_path: str):
    file_path = os.path.join(out_dir, full_path)

    # Serve file directly if it exists (e.g., /assets/index.js)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return FileResponse(file_path)

    # Otherwise, serve React index.html (for client-side routing)
    return FileResponse(os.path.join(out_dir, "index.html"))

# for the project's static folder done with react.js
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # get the absolut path of the file
OUT_DIR = os.path.join(BASE_DIR, "out")     # get the absolute path of the out folder
# print(os.path.exists(os.path.join(OUT_DIR, "index.html")))
# Mount static folder
app.mount("/assets", StaticFiles(directory=os.path.join(OUT_DIR, "assets")), name="assets")
'''
# for the project's static folder done with react/next.js
'''
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "out")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
OUT_DIR = os.path.join(STATIC_DIR, "out")

next_static_dir = os.path.join(OUT_DIR, "_next")
# create a directory get function for the static files
# BASE_DIR= os.path.dirname(os.path.abspath(__file__))
# STATIC_DIR = os.path.join(BASE_DIR, "static")
# env = Environment(loader=FileSystemLoader("templates"), cache_size=0)
#TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
# TEMPLATES_DIR.env = env

# mount the static files imported into the project
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if os.path.exists(OUT_DIR):
    app.mount("/", StaticFiles(directory=OUT_DIR, html=True), name="nextjs")

if os.path.exists(next_static_dir):
    app.mount("/_next", StaticFiles(directory=next_static_dir), name="next_static")


# recognize the html template sites for interaction
templates = Jinja2Templates(directory=TEMPLATES_DIR)
'''

# create the path for the react folder
# function for the home page

'''
@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """Serve homepage"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(OUT_DIR, "index.html"))
'
# write the function to serve the chat page
@app.get("/chat", response_class= HTMLResponse)
async def serve_chat(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})


# the function to serve the results page
@app.get("/results", response_class=HTMLResponse)
async def serve_results(request: Request):
    return templates.TemplateResponse("results.html", {"request": request})

'''
'''

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
'''