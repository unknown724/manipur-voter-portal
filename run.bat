@echo off
echo Starting Manipur Voter Search Portal...
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
pause
