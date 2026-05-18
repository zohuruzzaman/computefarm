@echo off
REM ComputeFarm - Purge pending jobs from the cpu queue.
REM Workers stay running; only PENDING tasks are deleted.
REM In-flight solves continue to completion.

cd /d %~dp0\framework
celery -A celery_app purge -Q cpu -f
pause
