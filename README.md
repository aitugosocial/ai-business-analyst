# The first business decision engine for solo founder

Lavoo is the very first gamified diagnostic and decision engine for the solo economy. Great business decisions can take days, months and years, now with Lavoo they take a few mins even seconds. This is the business doctor. 

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
