
# Define the database connections for the full application

# import the database creation engine
from sqlalchemy import create_engine

# import the function for the database mapping
from sqlalchemy.orm import declarative_base

# import the session controller \
from sqlalchemy.orm import sessionmaker

# import the operating system function
import os

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))   # get the absolute path of the file
db_path = os.path.join(os.path.join(CURRENT_DIR), "aitugo.db")
database_url = f"sqlite:///{db_path}"


# create the database
engine = create_engine(database_url, connect_args={"check_same_thread": False})

# create the session
SessionLocal = sessionmaker(bind = engine, autocommit = False, autoflush = False)

# declare the database mapping
Base = declarative_base()


# get the database session 
def get_db():
    db = SessionLocal()
    
    try:
        yield db
    finally:
        db.close()

