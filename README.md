# AI Business Analyst

A FastAPI-based app for recommending AI tools and providing business insights for creators/entrepreneurs.

## Setup
1. Clone: git clone https://github.com/aitugosocial/ai-business-analyst.git
2. cd ai-business-analyst
3. python3 -m venv venv
4. source venv/bin/activate.fish
5. pip install -r requirements.txt
6. Run: uvicorn api.main:app --reload

## Structure
- ai/: AI logic (recommendation, analyst)
- web/: Frontend
- db/: DB setup
- api/: FastAPI backend 
- tests/: Tests
