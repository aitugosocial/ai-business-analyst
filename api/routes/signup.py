# load the signup page

# import the fastAPI library into
from fastapi import FastAPI, Request, APIRouter,  Depends, Form, HTTPException

# import the function for rendering the HTML sites
from fastapi.responses import FileResponse, JSONResponse

# import the function for rendering the static files
from fastapi.staticfiles import StaticFiles

# import the function for rendering the Jinja files
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

# import the database files
from db.connections import get_db, SessionLocal

from pydantic import BaseModel

# import the session
from sqlalchemy.orm import Session

# import the user models for
from db.models import User

# import the function to hash the passwords
from passlib.context import CryptContext

import os

import traceback

router = APIRouter(
    prefix = "",
    tags = ['signup']
)

# call the function for hashing the user's password
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# for the project's static folder done with react.js
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # get the absolute path of the file
BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)))  # get the current directory of the file
OUT_DIR = os.path.join(BASE_DIR, "web")    # get the absolute path of the out folder

@router.post("/signup")
def signup(
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)):


    # Check if email exists
    if db.query(User).filter(User.email == email).first():
        return JSONResponse(status_code=400, content={"message": "User already exists"})

    # Create user
    hashed = pwd_context.hash(password)
    passcode = pwd_context.hash(confirm_password)
    new_user = User(name=name, email=email, password=hashed, confirm_password = passcode)

    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "User created successfully"}

'''
@router.get("/signup")
async def serve_home():
    return FileResponse(os.path.join(OUT_DIR, "index.html"))
'''
