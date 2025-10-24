
# Create models for the database data

# import the base model from the database file
from .connections import Base


# import the schema to enable columns and rows
from sqlalchemy import Column, Integer, String
from pydantic import BaseModel, ConfigDict
import os


# Create the model for the users to get in
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key = True, index = True)
    name = Column(String)
    email = Column(String)
    password = Column(String)
    confirm_password = Column(String)


''' Create the model for data retrieval from the database'''
class ShowUser(BaseModel):
    email: str
    password: str


'''Create model for returning user data'''
class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    email: str