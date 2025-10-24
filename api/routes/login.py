
from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from db.connections import get_db, SessionLocal


from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from db.models import User, ShowUser
from passlib.context import CryptContext
from jose import jwt, JWTError

from pydantic_settings import BaseSettings
import os, secrets

router = APIRouter(
    prefix = "",
    tags = ["authenticate"]
)

'''Generating and storing the secret key'''
class Settings(BaseSettings):
    secret_key: str = os.getenv("SECRET_KEY") or secrets.token_hex(32)

settings = Settings()
SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception

    return user
    
@router.post("/login")
def login(request: ShowUser, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    '''Check if the email is present'''
    if not user:
        raise HTTPException(status_code = status.HTTP_404_NOT_FOUND, 
                            detail = f'Email has not been registered!')

    #password = pwd_context.hash(request.password)
    if not pwd_context.verify(request.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password is incorrect!")
    

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}
    return {"message": "Login successful"}

# Add this new endpoint for Swagger UI OAuth2 form
@router.post("/token")
def login_for_swagger(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    OAuth2 compatible token login for Swagger UI.
    Use email as username in the form.
    """
    user = db.query(User).filter(User.email == form_data.username).first()
    
    if not user:
        print(f"User not found: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not pwd_context.verify(form_data.password, user.password):
        print(f"Password verification failed for: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.email},
        expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}